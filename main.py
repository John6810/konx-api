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

# Identifiants des Server Actions relevés dans le HAR. Ils dépendent de la
# version déployée de KONX : on tente de les re-scraper dynamiquement, avec
# ces valeurs en repli.
FALLBACK_BOOK_ACTION = "5a73d5a5fb24933178a6a7ef0b2519e8c087d8a7"
FALLBACK_CANCEL_ACTION = "2542a4d5c1b41e266c6c2c5f672586615764969e"


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if (v is not None and v.strip()) else default


KONX_SUPABASE_URL = env("KONX_SUPABASE_URL", "https://pyravfqvstegswptwraq.supabase.co")
KONX_ANON_KEY = env("KONX_ANON_KEY")
KONX_CLUB_ID = env("KONX_CLUB_ID", "a7ff5791-6ab7-447b-97d1-038d767693b0")
KONX_PROJECT_REF = KONX_SUPABASE_URL.split("//", 1)[-1].split(".", 1)[0]
AUTH_COOKIE = f"sb-{KONX_PROJECT_REF}-auth-token"

KAME_SUPABASE_URL = env("KAME_SUPABASE_URL")
KAME_SERVICE_ROLE_KEY = env("KAME_SERVICE_ROLE_KEY")
KAME_HOUSEHOLD_ID = env("KAME_HOUSEHOLD_ID")


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
    """grant_type=password → access/refresh tokens."""
    r = await client().post(
        f"{KONX_SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": KONX_ANON_KEY, "content-type": "application/json"},
        json={"email": acc.email, "password": acc.password},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"KONX login {acc.key}: {r.status_code} {r.text[:200]}")
    _store_session(acc, r.json())
    log.info("login ok: %s", acc.key)


async def _refresh(acc: Account) -> None:
    r = await client().post(
        f"{KONX_SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={"apikey": KONX_ANON_KEY, "content-type": "application/json"},
        json={"refresh_token": acc.refresh_token},
    )
    if r.status_code != 200:
        # refresh périmé → relogin complet
        await _login(acc)
        return
    _store_session(acc, r.json())


def _store_session(acc: Account, data: dict) -> None:
    acc.access_token = data["access_token"]
    acc.refresh_token = data.get("refresh_token", acc.refresh_token)
    acc.expires_at = datetime.now(timezone.utc).timestamp() + int(data.get("expires_in", 3600))
    # On garde la réponse Supabase entière : c'est elle (JSON encodé URL) qui
    # sert de valeur au cookie sb-...-auth-token attendu par KONX.
    acc.session = data


async def ensure_token(acc: Account, *, margin: float = 120.0) -> str:
    """Renvoie un access_token valide (login/refresh au besoin), thread-safe."""
    async with acc.lock:
        now = datetime.now(timezone.utc).timestamp()
        if not acc.access_token:
            await _login(acc)
        elif acc.expires_at - now < margin:
            await _refresh(acc)
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


async def book(acc: Account, session_id: str, date: str) -> bool:
    """Réserve un cours. HTTP 200 = succès (le corps est vide côté KONX)."""
    url = f"{KONX_BASE}/app/seance?t={session_id}&d={date}&club={KONX_CLUB_ID}"
    headers = await _action_headers(acc, FALLBACK_BOOK_ACTION, url)
    r = await client().post(url, headers=headers, content=json.dumps([session_id, date, False]))
    ok = r.status_code == 200 and "error" not in (r.text or "").lower()
    log.info("book %s %s %s -> %s", acc.key, session_id, date, r.status_code)
    return ok


async def cancel(acc: Account, booking_id: str, referer_session: str, date: str) -> bool:
    url = f"{KONX_BASE}/app/seance?t={referer_session}&d={date}&club={KONX_CLUB_ID}"
    headers = await _action_headers(acc, FALLBACK_CANCEL_ACTION, url)
    r = await client().post(url, headers=headers, content=json.dumps([booking_id]))
    return r.status_code == 200


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
        wait = (opening - now).total_seconds() - 5      # réveil 5 s avant
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
        deadline = datetime.now(TZ) + timedelta(seconds=8)
        booked = False
        while datetime.now(TZ) < deadline and not booked:
            booked = await book(acc, target["session_id"], date)
            if not booked:
                await asyncio.sleep(0.2)
        if booked:
            await mirror_to_kame(acc, rule.activity, date, target.get("start_time"), target.get("title"))
            log.info("✅ réservé %s %s %s", rule.user, rule.activity, date)
        else:
            log.warning("❌ échec réservation %s %s %s", rule.user, rule.activity, date)
        await asyncio.sleep(60)            # évite un re-tir immédiat


# --- Cycle de vie & endpoints ------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=15.0, headers={"user-agent": "konx-api/0.1"})
    rules = load_rules()
    for rule in rules:
        asyncio.create_task(snipe(rule))
    asyncio.create_task(classes_sync_loop())
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
async def cancel_ep(user: str, booking_id: str, session_id: str, date: str) -> dict:
    acc = account_or_404(user)
    return {"cancelled": await cancel(acc, booking_id, session_id, date)}
