"""
konx-api — proxy personnel de réservation KONX (crossfit / natation) pour le foyer.

Modèle « thin service » calqué sur resawod-api :
  - login multi-utilisateur (John, Lucie) via **Supabase Auth** (KONX est une app
    Supabase : on n'utilise donc PAS la Server Action de login de Next.js, mais
    le grant `password` standard de Supabase — stable, insensible aux mises à
    jour du site KONX) ;
  - lecture du planning, réservation, annulation via les Server Actions de KONX
    (avec le cookie de session Supabase) ;
  - un « sniper » qui réserve à la seconde près quand le créneau s'ouvre
    (ouverture = heure du cours − 24 h) ;
  - les cours réservés sont écrits dans le Supabase de Kame Hausu (table `events`
    typée `sport`) pour apparaître dans l'app, sans exposer d'endpoint public.

Secrets attendus (variables d'environnement) :
  KONX_SUPABASE_URL        https://pyravfqvstegswptwraq.supabase.co
  KONX_ANON_KEY            clé anon publique de KONX
  KONX_CLUB_ID             a7ff5791-6ab7-447b-97d1-038d767693b0
  KONX_JOHN_EMAIL / KONX_JOHN_PASSWORD
  KONX_LUCIE_EMAIL / KONX_LUCIE_PASSWORD
  KONX_JOHN_NAME=John / KONX_LUCIE_NAME=Lucie   (prénom = assignee côté Kame Hausu)
  KAME_SUPABASE_URL        https://jsrzqijlnzjtkztrzosr.supabase.co
  KAME_SERVICE_ROLE_KEY    clé service_role de Kame Hausu (écrit les events)
  KAME_HOUSEHOLD_ID        id du foyer dans Kame Hausu
  KONX_AUTOBOOK            JSON: règles d'auto-réservation (voir plus bas)

Deux points à finaliser lors du 1er déploiement contre le vrai KONX (marqués
« TODO(live) ») : le format exact du cookie `sb-...-auth-token` attendu par le
serveur KONX, et le parsing du planning (payload RSC). Le reste (auth Supabase,
sniper, POST des actions) découle directement du reverse-engineering du HAR.
"""
from __future__ import annotations
from urllib.parse import quote

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("konx-api")

TZ = ZoneInfo("Europe/Brussels")
KONX_BASE = "https://app.konx.be"

# Identifiants des Server Actions KONX (réserver / annuler). Ils vivent dans le
# bundle JS de KONX et sont stables tant que KONX ne rebuild pas. On les rend
# surchargeable par secret (KONX_BOOK_ACTION / KONX_CANCEL_ACTION) pour pouvoir
# corriger sans redéployer, et un auto-contrôle au démarrage alerte s'ils ont
# disparu du bundle (= KONX a changé de version).


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if (v is not None and v.strip()) else default


KONX_SUPABASE_URL = env("KONX_SUPABASE_URL", "https://pyravfqvstegswptwraq.supabase.co")
KONX_ANON_KEY = env("KONX_ANON_KEY")
KONX_CLUB_ID = env("KONX_CLUB_ID", "a7ff5791-6ab7-447b-97d1-038d767693b0")
FALLBACK_BOOK_ACTION = env("KONX_BOOK_ACTION", "5a73d5a5fb24933178a6a7ef0b2519e8c087d8a7")
FALLBACK_CANCEL_ACTION = env("KONX_CANCEL_ACTION", "2542a4d5c1b41e266c6c2c5f672586615764969e")
KONX_PROJECT_REF = KONX_SUPABASE_URL.split("//", 1)[-1].split(".", 1)[0]
AUTH_COOKIE = f"sb-{KONX_PROJECT_REF}-auth-token"

KAME_SUPABASE_URL = env("KAME_SUPABASE_URL")
KAME_SERVICE_ROLE_KEY = env("KAME_SERVICE_ROLE_KEY")
KAME_HOUSEHOLD_ID = env("KAME_HOUSEHOLD_ID")
KAME_APP_URL = env("KAME_APP_URL", "https://kame.jonathan-aerts.dev")


@dataclass
class Account:
    key: str            # "john" | "lucie"
    email: str
    password: str
    name: str           # prénom = assignee côté Kame Hausu
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float = 0.0        # epoch s
    session: dict | None = None    # réponse Supabase complète (= valeur du cookie)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def load_accounts() -> dict[str, Account]:
    accounts: dict[str, Account] = {}
    for key in ("john", "lucie"):
        email = env(f"KONX_{key.upper()}_EMAIL")
        password = env(f"KONX_{key.upper()}_PASSWORD")
        if email and password:
            accounts[key] = Account(
                key=key,
                email=email,
                password=password,
                name=env(f"KONX_{key.upper()}_NAME", key.capitalize()),
            )
    return accounts


ACCOUNTS = load_accounts()
app = FastAPI(title="konx-api", version="0.1.0")
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    assert _client is not None
    return _client


# --- Auth Supabase -----------------------------------------------------------

async def _login(acc: Account) -> None:
    """grant_type=password → nouvelle session (éjecte la session du tél si KONX
    est en single-session). On l'évite au maximum grâce au refresh persistant."""
    r = await client().post(
        f"{KONX_SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": KONX_ANON_KEY, "content-type": "application/json"},
        json={"email": acc.email, "password": acc.password},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"KONX login {acc.key}: {r.status_code} {r.text[:200]}")
    _store_session(acc, r.json())
    await _persist_session(acc)
    log.info("login (mot de passe) ok: %s", acc.key)


async def _try_refresh(acc: Account) -> bool:
    """Rafraîchit le jeton SANS créer de nouvelle session (ne déconnecte pas le
    tél). Renvoie False si le refresh token n'est plus valide."""
    if not acc.refresh_token:
        return False
    r = await client().post(
        f"{KONX_SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={"apikey": KONX_ANON_KEY, "content-type": "application/json"},
        json={"refresh_token": acc.refresh_token},
    )
    if r.status_code != 200:
        return False
    _store_session(acc, r.json())
    await _persist_session(acc)
    return True


def _store_session(acc: Account, data: dict) -> None:
    acc.access_token = data["access_token"]
    acc.refresh_token = data.get("refresh_token", acc.refresh_token)
    acc.expires_at = datetime.now(timezone.utc).timestamp() + int(data.get("expires_in", 3600))
    # On garde la réponse Supabase entière : c'est elle (JSON encodé URL) qui
    # sert de valeur au cookie sb-...-auth-token attendu par KONX.
    acc.session = data


async def _persist_session(acc: Account) -> None:
    """Sauve la session dans Kame Hausu (illisible côté client) pour survivre
    à un redémarrage du pod sans reconnexion au mot de passe."""
    if not (KAME_SUPABASE_URL and KAME_SERVICE_ROLE_KEY and acc.session):
        return
    try:
        await client().post(
            f"{KAME_SUPABASE_URL}/rest/v1/konx_sessions",
            params={"on_conflict": "account_key"},
            headers={**_kame_headers(), "prefer": "resolution=merge-duplicates,return=minimal"},
            content=json.dumps({
                "account_key": acc.key,
                "session": acc.session,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("persist_session %s: %s", acc.key, e)


async def _load_saved_session(acc: Account) -> bool:
    """Recharge la session sauvée (refresh token) au démarrage. Renvoie False
    si rien de sauvé."""
    if not (KAME_SUPABASE_URL and KAME_SERVICE_ROLE_KEY):
        return False
    try:
        r = await client().get(
            f"{KAME_SUPABASE_URL}/rest/v1/konx_sessions",
            params={"account_key": f"eq.{acc.key}", "select": "session"},
            headers=_kame_headers(),
        )
        rows = r.json() if r.status_code < 300 else []
        if not rows:
            return False
        data = rows[0]["session"]
        acc.session = data
        acc.access_token = data.get("access_token")
        acc.refresh_token = data.get("refresh_token")
        acc.expires_at = 0.0  # force un refresh immédiat (le token stocké peut être périmé)
        return bool(acc.refresh_token)
    except Exception as e:  # noqa: BLE001
        log.warning("load_session %s: %s", acc.key, e)
        return False


async def ensure_token(acc: Account, *, margin: float = 120.0) -> str:
    """
    Renvoie un access_token valide. Ordre de préférence pour NE PAS déconnecter
    le tél : jeton en mémoire → refresh (session persistée) → login mot de passe.
    """
    async with acc.lock:
        now = datetime.now(timezone.utc).timestamp()
        if acc.access_token and acc.expires_at - now >= margin:
            return acc.access_token
        # 1) refresh en mémoire ; 2) refresh via session sauvée ; 3) login mdp.
        if await _try_refresh(acc):
            return acc.access_token  # type: ignore[return-value]
        if not acc.access_token and await _load_saved_session(acc) and await _try_refresh(acc):
            log.info("session restaurée (refresh) : %s — pas de reconnexion mot de passe", acc.key)
            return acc.access_token  # type: ignore[return-value]
        await _login(acc)
        return acc.access_token  # type: ignore[return-value]


def auth_cookie(acc: Account) -> str:
    """
    Cookie de session attendu par le serveur KONX (@supabase/ssr).
    Format confirmé en conditions réelles : la réponse Supabase COMPLÈTE
    (access_token, refresh_token, expires_at, user…), en JSON encodé URL, sur
    un seul cookie. Un objet tronqué est refusé (redirection 307 vers /login).
    """
    return f"{AUTH_COOKIE}={quote(json.dumps(acc.session, separators=(',', ':')))}"



def account_or_404(user: str) -> Account:
    acc = ACCOUNTS.get(user.lower())
    if not acc:
        raise HTTPException(404, f"Utilisateur inconnu : {user}")
    return acc


# --- Server Actions KONX (planning / réservation / annulation) ---------------

async def _action_headers(acc: Account, action_id: str, referer: str) -> dict:
    token = await ensure_token(acc)
    acc.access_token = token
    return {
        "cookie": auth_cookie(acc),
        "next-action": action_id,
        "content-type": "text/plain;charset=UTF-8",
        "referer": referer,
        "origin": KONX_BASE,
    }


async def fetch_planning(acc: Account, date: str) -> list[dict]:
    """
    Cours d'un jour. TODO(live): parser le payload RSC de
    GET /app/planning?d=YYYY-MM-DD pour extraire, par cours :
    {session_id (t=), activity, start_time, capacity, booked, is_booked}.
    """
    token = await ensure_token(acc)
    acc.access_token = token
    r = await client().get(
        f"{KONX_BASE}/app/planning",
        params={"d": date},
        headers={"cookie": auth_cookie(acc)},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"planning {acc.key} {date}: {r.status_code}")
    return _parse_planning(r.text)  # à compléter en conditions réelles


_CARD_RE = re.compile(r'<a class="card[^>]*?href="(/app/seance\?[^"]+)"[\s\S]*?</a>')
_TIME_RE = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d")


def _parse_planning(html: str) -> list[dict]:
    """
    Chaque cours est une carte <a href="/app/seance?t=…&d=…&club=…"> contenant
    l'heure de début/fin, le nom du cours, la jauge « booked/capacity » et un
    éventuel cadenas (réservations pas encore ouvertes).
    """
    out: list[dict] = []
    for m in _CARD_RE.finditer(html):
        href = m.group(1).replace("&amp;", "&")
        sid = re.search(r"[?&]t=([0-9a-f-]{36})", href)
        if not sid:
            continue
        block = m.group(0)
        text = re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).replace("&amp;", "&").strip()
        times = _TIME_RE.findall(text)
        cap = re.search(r"(\d+)\s*/\s*(\d+)", text)
        # Nom du cours : le <h3> de la carte (ex. "Wod", "Hyrox (1h)", "Haltero",
        # "Functional Body Building"). Fiable, contrairement au texte positionnel.
        h3 = re.search(r"<h3[^>]*>([\s\S]*?)</h3>", block)
        title = re.sub(r"<[^>]+>", " ", h3.group(1)) if h3 else ""
        title = re.sub(r"\s+", " ", title).replace("&amp;", "&").strip()
        out.append({
            "session_id": sid.group(1),
            "activity": "natation" if "natation" in title.lower() else "crossfit",
            "title": title or None,
            "start_time": times[0] if times else None,
            "end_time": times[1] if len(times) > 1 else None,
            "booked": int(cap.group(1)) if cap else None,
            "capacity": int(cap.group(2)) if cap else None,
            "locked": "\U0001f512" in text,   # 🔒 réservations pas encore ouvertes
        })
    return out


async def is_registered(acc: Account, session_id: str, date: str) -> bool:
    """Vérifie l'inscription réelle : KONX renvoie 200 même trop tôt / en échec,
    donc on regarde l'état de la séance (« inscrit » / « désinscrire »)."""
    url = f"{KONX_BASE}/app/seance?t={session_id}&d={date}&club={KONX_CLUB_ID}"
    r = await client().get(url, headers={"cookie": auth_cookie(acc)})
    if r.status_code != 200:
        return False
    txt = re.sub(r"<[^>]+>", " ", r.text).lower()
    return ("désinscrire" in txt) or ("annuler ma" in txt) or (" inscrit" in txt)


# Id d'action « réserver » courant : part de la valeur connue, mais peut être
# ré-appris automatiquement si KONX a rebuild (voir auto_repair_book).
_book_action = FALLBACK_BOOK_ACTION
_cancel_action = FALLBACK_CANCEL_ACTION


async def scrape_action_ids(acc: Account, t: str, date: str) -> list[str]:
    """
    Ids des Server Actions du bundle de la route /app/seance. On récupère l'URL
    du chunk sur une page séance AUTHENTIFIÉE (sinon KONX redirige vers /login),
    puis on lit le chunk (asset public).
    """
    try:
        url = f"{KONX_BASE}/app/seance?t={t}&d={date}&club={KONX_CLUB_ID}"
        await ensure_token(acc)
        page = (await client().get(url, headers={"cookie": auth_cookie(acc)})).text
        m = re.search(r"/_next/static/chunks/app/app/seance/[\w.-]+\.js", page)
        if not m:
            return []
        js = (await client().get(f"{KONX_BASE}{m.group(0)}")).text
        # (0,n.$)("<hash>") = createServerReference
        return list(dict.fromkeys(re.findall(r'\$\)\("([0-9a-f]{40})"\)', js)))
    except Exception as e:  # noqa: BLE001
        log.warning("scrape_action_ids: %s", e)
        return []


async def _post_book(acc: Account, session_id: str, date: str, action_id: str) -> bool:
    url = f"{KONX_BASE}/app/seance?t={session_id}&d={date}&club={KONX_CLUB_ID}"
    headers = await _action_headers(acc, action_id, url)
    r = await client().post(url, headers=headers, content=json.dumps([session_id, date, False]))
    return r.status_code == 200 and await is_registered(acc, session_id, date)


async def book(acc: Account, session_id: str, date: str) -> bool:
    """Réserve avec l'id connu et VÉRIFIE l'inscription (un 200 ne suffit pas)."""
    ok = await _post_book(acc, session_id, date, _book_action)
    log.info("book %s %s %s -> inscrit=%s", acc.key, session_id, date, ok)
    return ok


async def auto_repair_book(acc: Account, session_id: str, date: str) -> bool:
    """
    Si l'id connu ne réserve plus (rebuild KONX), on essaie les autres ids du
    bundle en vérifiant l'inscription à chaque fois — un mauvais id ne peut donc
    pas « faussement réussir ». Le premier qui inscrit devient le nouvel id.
    """
    global _book_action
    candidates = [a for a in await scrape_action_ids(acc, session_id, date) if a != _book_action]
    for aid in candidates:
        if await _post_book(acc, session_id, date, aid):
            log.warning("auto-repair: nouvel id de réservation %s (ancien %s)", aid[:12], _book_action[:12])
            _book_action = aid
            return True
    return False


async def cancel(acc: Account, t: str, date: str) -> bool:
    """
    Annule la réservation du cours `t`. KONX attend l'id INTERNE de la séance
    (champ `session_id` de la page, différent de l'id public `t=`), pas un id
    de réservation. On le lit sur la page, puis on POST l'action d'annulation.
    """
    url = f"{KONX_BASE}/app/seance?t={t}&d={date}&club={KONX_CLUB_ID}"
    await ensure_token(acc)
    page = (await client().get(url, headers={"cookie": auth_cookie(acc)})).text
    if "ésinscri" not in page.lower():
        return True  # déjà pas inscrit → rien à annuler
    i = page.find("session_id")
    m = re.search(r"[0-9a-f-]{36}", page[i : i + 60]) if i >= 0 else None
    if not m:
        log.warning("cancel %s %s : session_id interne introuvable", acc.key, t)
        return False
    internal = m.group(0)

    async def try_cancel(action_id: str) -> bool:
        headers = await _action_headers(acc, action_id, url)
        r = await client().post(url, headers=headers, content=json.dumps([internal]))
        if r.status_code != 200:
            return False
        after = (await client().get(url, headers={"cookie": auth_cookie(acc)})).text
        return "ésinscri" not in after.lower()

    global _cancel_action
    if await try_cancel(_cancel_action):
        return True
    # Id d'annulation périmé (rebuild KONX) : on ré-apprend, avec vérification.
    for aid in (a for a in await scrape_action_ids(acc, t, date) if a != _cancel_action):
        if await try_cancel(aid):
            log.warning("auto-repair: nouvel id d'annulation %s (ancien %s)", aid[:12], _cancel_action[:12])
            _cancel_action = aid
            return True
    return False


# --- Mirroring vers Kame Hausu ----------------------------------------------

async def mirror_to_kame(
    acc: Account, activity: str, date: str, time_: str | None, title: str | None = None
) -> None:
    """Écrit le cours réservé dans le Supabase de Kame Hausu (events / sport)."""
    if not (KAME_SUPABASE_URL and KAME_SERVICE_ROLE_KEY and KAME_HOUSEHOLD_ID):
        return
    emoji = "🏊" if activity == "natation" else "🏋️"
    label = "Natation" if activity == "natation" else "CrossFit"
    row = {
        "household_id": KAME_HOUSEHOLD_ID,
        # Nom réel du cours (Wod, Hyrox, Haltero…) s'il est connu, sinon générique.
        "title": title or label,
        "event_date": date,
        "event_time": time_,
        "assignee": acc.name,
        "emoji": emoji,
        "color": "#F97316" if activity != "natation" else "#0EA5E9",
        "category": "sport",
        "sport_activity": activity,
        "created_by_name": acc.name,
    }
    await client().post(
        f"{KAME_SUPABASE_URL}/rest/v1/events",
        headers={
            "apikey": KAME_SERVICE_ROLE_KEY,
            "authorization": f"Bearer {KAME_SERVICE_ROLE_KEY}",
            "content-type": "application/json",
            "prefer": "return=minimal",
        },
        content=json.dumps(row),
    )


# --- Catalogue « cours de la semaine » (agenda CrossFit) ---------------------
#
# Le dimanche, dès que la box publie le planning de la semaine à venir, on le
# copie dans la table sport_classes de Kame Hausu (l'app l'affiche en lecture ;
# un cours ne devient un event perso que si l'utilisateur clique « j'y vais »).

def _kame_headers() -> dict:
    return {
        "apikey": KAME_SERVICE_ROLE_KEY,
        "authorization": f"Bearer {KAME_SERVICE_ROLE_KEY}",
        "content-type": "application/json",
    }


def next_monday(now: datetime) -> str:
    """Lundi de la semaine À VENIR (le dimanche, c'est demain)."""
    days = (7 - now.weekday()) % 7 or 7
    return (now.date() + timedelta(days=days)).isoformat()


async def week_published(acc: Account, monday: str) -> bool:
    """La box a-t-elle publié la semaine ? (≥ 1 cours le lundi visé)"""
    try:
        return len(await fetch_planning(acc, monday)) > 0
    except Exception:  # noqa: BLE001
        return False


async def sync_week_classes(acc: Account, monday: str) -> int:
    """Copie les 7 jours de la semaine `monday` dans sport_classes (upsert)."""
    if not (KAME_SUPABASE_URL and KAME_SERVICE_ROLE_KEY and KAME_HOUSEHOLD_ID):
        raise HTTPException(503, "Supabase Kame Hausu non configuré.")
    rows: list[dict] = []
    start = datetime.fromisoformat(monday).date()
    for i in range(7):
        day = (start + timedelta(days=i)).isoformat()
        for c in await fetch_planning(acc, day):
            rows.append({
                "household_id": KAME_HOUSEHOLD_ID,
                "konx_session_id": c["session_id"],
                "club_id": KONX_CLUB_ID,
                "class_date": day,
                "start_time": c.get("start_time"),
                "end_time": c.get("end_time"),
                "activity": c.get("activity", "crossfit"),
                "title": c.get("title"),
                "capacity": c.get("capacity"),
                "booked": c.get("booked"),
                "locked": bool(c.get("locked")),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })
    if rows:
        r = await client().post(
            f"{KAME_SUPABASE_URL}/rest/v1/sport_classes",
            params={"on_conflict": "household_id,konx_session_id"},
            headers={**_kame_headers(), "prefer": "resolution=merge-duplicates,return=minimal"},
            content=json.dumps(rows),
        )
        if r.status_code >= 300:
            raise HTTPException(502, f"upsert sport_classes: {r.status_code} {r.text[:200]}")
    # Ménage : on retire les cours passés.
    today = datetime.now(TZ).date().isoformat()
    await client().request(
        "DELETE",
        f"{KAME_SUPABASE_URL}/rest/v1/sport_classes",
        params={"household_id": f"eq.{KAME_HOUSEHOLD_ID}", "class_date": f"lt.{today}"},
        headers={**_kame_headers(), "prefer": "return=minimal"},
    )
    return len(rows)


async def classes_sync_loop() -> None:
    """Le dimanche, réessaie toutes les 30 min jusqu'à trouver la semaine publiée."""
    acc = ACCOUNTS.get("john") or (next(iter(ACCOUNTS.values())) if ACCOUNTS else None)
    if not acc:
        return
    synced_for: str | None = None
    while True:
        try:
            now = datetime.now(TZ)
            target = next_monday(now)
            if now.weekday() == 6 and synced_for != target:   # dimanche, pas encore fait
                if await week_published(acc, target):
                    n = await sync_week_classes(acc, target)
                    synced_for = target
                    log.info("📅 cours de la semaine du %s copiés (%d)", target, n)
                else:
                    log.info("semaine du %s pas encore publiée, nouvel essai dans 30 min", target)
        except Exception as e:  # noqa: BLE001
            log.error("classes_sync_loop: %s", e)
        await asyncio.sleep(1800)   # 30 min


# --- Réservation depuis l'app (« j'y vais ») ---------------------------------
#
# L'app pose un event sport avec konx_session_id + konx_booking_status='pending'.
# Ici on récupère ces intentions et on réserve pile à l'ouverture (début − 24 h),
# puis on repasse le statut à 'booked' / 'failed'.

def account_for_person(person: str | None) -> Account | None:
    if not person:
        return None
    p = person.strip().lower()
    for acc in ACCOUNTS.values():
        if acc.name.strip().lower() == p or acc.key == p:
            return acc
    return None


async def _kame_event(event_id: str) -> dict | None:
    r = await client().get(
        f"{KAME_SUPABASE_URL}/rest/v1/events",
        params={"id": f"eq.{event_id}", "select": "id,konx_booking_status"},
        headers=_kame_headers(),
    )
    rows = r.json() if r.status_code < 300 else []
    return rows[0] if rows else None


async def _set_booking_status(event_id: str, status: str) -> None:
    await client().patch(
        f"{KAME_SUPABASE_URL}/rest/v1/events",
        params={"id": f"eq.{event_id}"},
        headers={**_kame_headers(), "prefer": "return=minimal"},
        content=json.dumps({"konx_booking_status": status}),
    )


async def notify_booked(person: str | None, title: str, date: str, time_: str | None) -> None:
    """Prévient la personne que sa séance est réservée (via le Worker Kame Hausu)."""
    if not (KAME_APP_URL and KAME_SERVICE_ROLE_KEY and KAME_HOUSEHOLD_ID and person):
        return
    when = date
    try:
        d = datetime.fromisoformat(date).date()
        when = d.strftime("%d/%m")
    except Exception:  # noqa: BLE001
        pass
    body = f"{title}{(' ' + time_[:5]) if time_ else ''} le {when} — c'est réservé sur KONX 💪"
    try:
        await client().post(
            f"{KAME_APP_URL}/api/push/konx",
            headers={"authorization": f"Bearer {KAME_SERVICE_ROLE_KEY}", "content-type": "application/json"},
            content=json.dumps({
                "householdId": KAME_HOUSEHOLD_ID,
                "toName": person,
                "title": "✅ Séance réservée",
                "body": body,
                "url": "/sport",
            }),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("notify_booked KO: %s", e)


async def notify_failed(person: str | None, title: str, date: str, time_: str | None) -> None:
    """Prévient la personne qu'une réservation a échoué (à faire à la main)."""
    if not (KAME_APP_URL and KAME_SERVICE_ROLE_KEY and KAME_HOUSEHOLD_ID and person):
        return
    when = date
    try:
        when = datetime.fromisoformat(date).date().strftime("%d/%m")
    except Exception:  # noqa: BLE001
        pass
    body = f"{title}{(' ' + time_[:5]) if time_ else ''} le {when} : impossible de réserver — réserve à la main sur KONX."
    try:
        await client().post(
            f"{KAME_APP_URL}/api/push/konx",
            headers={"authorization": f"Bearer {KAME_SERVICE_ROLE_KEY}", "content-type": "application/json"},
            content=json.dumps({
                "householdId": KAME_HOUSEHOLD_ID,
                "toName": person,
                "title": "⚠️ Réservation ratée",
                "body": body,
                "url": "/sport/cours",
            }),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("notify_failed KO: %s", e)


async def snipe_intent(ev: dict) -> None:
    event_id = ev["id"]
    acc = account_for_person(ev.get("assignee"))
    session_id = ev.get("konx_session_id")
    date = ev.get("event_date")
    start = (ev.get("event_time") or "00:00")[:5]
    if not (acc and session_id and date):
        await _set_booking_status(event_id, "failed")
        log.warning("intent %s: compte/cours introuvable (assignee=%s)", event_id, ev.get("assignee"))
        return

    cls = datetime.fromisoformat(f"{date}T{start}:00").replace(tzinfo=TZ)
    opening = cls - timedelta(hours=24)
    now = datetime.now(TZ)
    wait = (opening - now).total_seconds() - 2
    log.info("intent %s (%s %s %s): ouverture %s (dans %.0f s)", event_id, acc.key, session_id[:8],
             date, opening.isoformat(timespec="minutes"), max(wait, 0))
    if wait > 0:
        await asyncio.sleep(wait)

    # L'utilisateur a pu se désinscrire entre-temps : on revérifie.
    fresh = await _kame_event(event_id)
    if not fresh or fresh.get("konx_booking_status") != "pending":
        log.info("intent %s annulée avant l'ouverture, on n'y touche pas", event_id)
        return

    await ensure_token(acc)
    deadline = datetime.now(TZ) + timedelta(seconds=20)
    booked = False
    while datetime.now(TZ) < deadline and not booked:
        booked = await book(acc, session_id, date)
        if not booked:
            await asyncio.sleep(0.5)
    # Échec avec l'id connu : peut-être un rebuild KONX → on ré-apprend l'id.
    if not booked:
        booked = await auto_repair_book(acc, session_id, date)

    await _set_booking_status(event_id, "booked" if booked else "failed")
    log.info("intent %s -> %s", event_id, "✅ booked" if booked else "❌ failed")
    title = ev.get("title") or "Séance"
    if booked:
        await notify_booked(ev.get("assignee"), title, date, ev.get("event_time"))
    else:
        await notify_failed(ev.get("assignee"), title, date, ev.get("event_time"))


_scheduled_intents: set[str] = set()


async def booking_intents_loop() -> None:
    """Récupère les « j'y vais » en attente et programme leur réservation."""
    if not (KAME_SUPABASE_URL and KAME_SERVICE_ROLE_KEY):
        return
    while True:
        try:
            today = datetime.now(TZ).date().isoformat()
            r = await client().get(
                f"{KAME_SUPABASE_URL}/rest/v1/events",
                params={
                    "select": "id,assignee,title,konx_session_id,event_date,event_time,konx_booking_status",
                    "category": "eq.sport",
                    "konx_booking_status": "eq.pending",
                    "event_date": f"gte.{today}",
                },
                headers=_kame_headers(),
            )
            for ev in (r.json() if r.status_code < 300 else []):
                if ev["id"] not in _scheduled_intents:
                    _scheduled_intents.add(ev["id"])
                    asyncio.create_task(_run_intent(ev))

            # Demandes d'annulation (« se retirer » dans l'app) : on annule sur
            # KONX puis on marque la séance « zappée » — elle reste dans
            # l'historique au lieu de disparaître.
            rc = await client().get(
                f"{KAME_SUPABASE_URL}/rest/v1/events",
                params={
                    "select": "id,assignee,konx_session_id,event_date",
                    "category": "eq.sport",
                    "konx_booking_status": "eq.cancel",
                },
                headers=_kame_headers(),
            )
            for ev in (rc.json() if rc.status_code < 300 else []):
                await _process_cancel(ev)
        except Exception as e:  # noqa: BLE001
            log.error("booking_intents_loop: %s", e)
        await asyncio.sleep(60)


async def _kame_mark_skipped(event_id: str) -> None:
    """Annulation faite : la séance reste dans l'agenda, marquée « zappée ».

    On la supprimait, et le sport annulé disparaissait de l'historique — donc
    des statistiques de la semaine aussi. La garder dit ce qui s'est passé :
    c'était prévu, on n'y est pas allé.
    """
    await client().patch(
        f"{KAME_SUPABASE_URL}/rest/v1/events",
        params={"id": f"eq.{event_id}"},
        headers={**_kame_headers(), "prefer": "return=minimal"},
        json={"sport_status": "skipped", "konx_booking_status": None},
    )


async def _process_cancel(ev: dict) -> None:
    acc = account_for_person(ev.get("assignee"))
    t = ev.get("konx_session_id")
    date = ev.get("event_date")
    if not (acc and t and date):
        # Rien à annuler côté KONX (compte/cours inconnu) : on marque quand même.
        await _kame_mark_skipped(ev["id"])
        return
    try:
        ok = await cancel(acc, t, date)
        log.info("annulation %s %s %s -> %s", acc.key, t[:8], date, "✅" if ok else "❌")
    except Exception as e:  # noqa: BLE001
        log.error("_process_cancel %s: %s", ev["id"], e)
        return  # on réessaiera au prochain passage (event garde le statut 'cancel')
    await _kame_mark_skipped(ev["id"])


async def _run_intent(ev: dict) -> None:
    try:
        await snipe_intent(ev)
    finally:
        _scheduled_intents.discard(ev["id"])


# --- Sniper : réserve à l'ouverture (cours − 24 h) ---------------------------

@dataclass
class AutoRule:
    user: str
    weekday: int      # 0 = lundi … 6 = dimanche (jour DU COURS)
    time: str         # "HH:MM" heure du cours
    activity: str     # "crossfit" | "natation"
    lead_hours: int = 24   # ouverture = cours − lead_hours


def load_rules() -> list[AutoRule]:
    raw = env("KONX_AUTOBOOK")
    if not raw:
        return []
    try:
        return [AutoRule(**r) for r in json.loads(raw)]
    except Exception as e:  # noqa: BLE001
        log.error("KONX_AUTOBOOK invalide: %s", e)
        return []


def next_class_dt(rule: AutoRule, now: datetime) -> datetime:
    """Prochaine occurrence (aware, TZ) du cours pour cette règle."""
    hh, mm = (int(x) for x in rule.time.split(":"))
    days_ahead = (rule.weekday - now.weekday()) % 7
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=days_ahead)
    opening = cand - timedelta(hours=rule.lead_hours)
    if opening <= now:                    # ouverture déjà passée → semaine suivante
        cand += timedelta(days=7)
    return cand


async def snipe(rule: AutoRule) -> None:
    acc = ACCOUNTS.get(rule.user.lower())
    if not acc:
        log.warning("règle ignorée, utilisateur inconnu: %s", rule.user)
        return
    while True:
        now = datetime.now(TZ)
        cls = next_class_dt(rule, now)
        opening = cls - timedelta(hours=rule.lead_hours)
        wait = (opening - now).total_seconds() - 2      # réveil 2 s avant
        log.info("snipe %s %s %s: ouverture %s (dans %.0f s)", rule.user, rule.activity,
                 cls.date(), opening.isoformat(timespec="minutes"), max(wait, 0))
        if wait > 0:
            await asyncio.sleep(wait)

        date = cls.date().isoformat()
        # On récupère l'id du cours du jour visé, puis on martèle autour de T.
        try:
            classes = await fetch_planning(acc, date)
        except Exception as e:  # noqa: BLE001
            log.error("snipe planning KO: %s", e)
            classes = []
        target = next(
            (c for c in classes if c.get("activity") == rule.activity and (c.get("start_time") or "").startswith(rule.time)),
            None,
        )
        if not target:
            log.warning("cours introuvable pour %s %s %s (parsing à finaliser)", rule.user, rule.activity, date)
            await asyncio.sleep(60)
            continue

        await ensure_token(acc)            # token chaud
        deadline = datetime.now(TZ) + timedelta(seconds=20)
        booked = False
        while datetime.now(TZ) < deadline and not booked:
            booked = await book(acc, target["session_id"], date)
            if not booked:
                await asyncio.sleep(0.5)
        if not booked:
            booked = await auto_repair_book(acc, target["session_id"], date)
        if booked:
            await mirror_to_kame(acc, rule.activity, date, target.get("start_time"), target.get("title"))
            log.info("✅ réservé %s %s %s", rule.user, rule.activity, date)
        else:
            log.warning("❌ échec réservation %s %s %s", rule.user, rule.activity, date)
            await notify_failed(acc.name, target.get("title") or rule.activity, date, target.get("start_time"))
        await asyncio.sleep(60)            # évite un re-tir immédiat


# --- Cycle de vie & endpoints ------------------------------------------------

async def verify_actions() -> None:
    """Alerte si les ids d'action ne sont plus dans le bundle KONX (rebuild)."""
    acc = next(iter(ACCOUNTS.values()), None)
    if not acc:
        return
    try:
        today = datetime.now(TZ).date().isoformat()
        classes = await fetch_planning(acc, today)
        if not classes:
            log.info("verify_actions: pas de cours aujourd'hui pour sonder le bundle")
            return
        ids = await scrape_action_ids(acc, classes[0]["session_id"], today)
        for name, aid, var in (("réserver", _book_action, "BOOK"), ("annuler", _cancel_action, "CANCEL")):
            if ids and aid not in ids:
                log.error("⚠️ action « %s » (%s) absente du bundle KONX — sera ré-apprise, ou fixe KONX_%s_ACTION",
                          name, aid[:12], var)
            else:
                log.info("action « %s » ok (%s)", name, aid[:12])
    except Exception as e:  # noqa: BLE001
        log.warning("verify_actions: %s", e)


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=15.0, headers={"user-agent": "konx-api/0.1"})
    rules = load_rules()
    for rule in rules:
        asyncio.create_task(snipe(rule))
    asyncio.create_task(classes_sync_loop())
    asyncio.create_task(booking_intents_loop())
    asyncio.create_task(verify_actions())
    log.info("konx-api démarré · comptes=%s · règles=%d", list(ACCOUNTS), len(rules))


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client:
        await _client.aclose()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "accounts": list(ACCOUNTS), "rules": len(load_rules())}


@app.get("/{user}/planning")
async def planning(user: str, d: str) -> dict:
    acc = account_or_404(user)
    return {"date": d, "classes": await fetch_planning(acc, d)}


@app.post("/sync/classes")
async def sync_classes_ep(monday: str | None = None) -> dict:
    """Déclenche un sync manuel du catalogue (par défaut : semaine à venir)."""
    acc = ACCOUNTS.get("john") or (next(iter(ACCOUNTS.values())) if ACCOUNTS else None)
    if not acc:
        raise HTTPException(503, "Aucun compte configuré.")
    target = monday or next_monday(datetime.now(TZ))
    return {"week": target, "synced": await sync_week_classes(acc, target)}


@app.post("/{user}/book")
async def book_ep(user: str, session_id: str, date: str) -> dict:
    acc = account_or_404(user)
    return {"booked": await book(acc, session_id, date)}


@app.delete("/{user}/book")
async def cancel_ep(user: str, session_id: str, date: str) -> dict:
    acc = account_or_404(user)
    return {"cancelled": await cancel(acc, session_id, date)}
