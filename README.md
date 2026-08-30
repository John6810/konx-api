# 🏋️ konx-api

Proxy personnel de réservation **KONX** (crossfit / natation) pour le foyer,
sur le modèle de `resawod-api`. Il se connecte aux comptes KONX, réserve les
cours **à la seconde près** dès l'ouverture (cours − 24 h), et recopie les
séances réservées dans le Supabase de **Kame Hausu** (elles apparaissent alors
dans le module Sport de l'app, sans endpoint public).

> ⚠️ KONX est le logiciel d'un tiers, sans API publique. Ce service reproduit
> les appels de l'app web (auth **Supabase**, Server Actions Next.js). Deux
> détails sont à confirmer au 1er déploiement (voir « À finaliser »).

## Architecture

```
konx-api (k8s, namespace konx-api)
  ├─ Auth : Supabase password grant sur le projet KONX (par utilisateur)
  ├─ Réserve/annule via les Server Actions KONX (/app/seance)
  ├─ Sniper : réveil 5 s avant l'ouverture, tir serré ~8 s @200 ms
  └─ Mirroring : écrit les cours réservés dans Supabase Kame Hausu (events/sport)
```

Interne (ClusterIP) : il n'expose rien publiquement. Le lien avec l'app se fait
uniquement par écriture dans la base Kame Hausu.

## Secrets Kubernetes (namespace `konx-api`)

Trois secrets, montés via `envFrom` :

```bash
# 1) Config commune
kubectl -n konx-api create secret generic konx-config \
  --from-literal=KONX_SUPABASE_URL=https://pyravfqvstegswptwraq.supabase.co \
  --from-literal=KONX_ANON_KEY='<clé anon publique de KONX>' \
  --from-literal=KONX_CLUB_ID=a7ff5791-6ab7-447b-97d1-038d767693b0 \
  --from-literal=KAME_SUPABASE_URL=https://jsrzqijlnzjtkztrzosr.supabase.co \
  --from-literal=KAME_SERVICE_ROLE_KEY='<service_role Kame Hausu>' \
  --from-literal=KAME_HOUSEHOLD_ID='<id du foyer>' \
  --from-literal=KONX_AUTOBOOK='<voir plus bas>'

# 2) Compte John
kubectl -n konx-api create secret generic konx-john \
  --from-literal=KONX_JOHN_EMAIL='...' \
  --from-literal=KONX_JOHN_PASSWORD='...' \
  --from-literal=KONX_JOHN_NAME=John

# 3) Compte Lucie
kubectl -n konx-api create secret generic konx-lucie \
  --from-literal=KONX_LUCIE_EMAIL='...' \
  --from-literal=KONX_LUCIE_PASSWORD='...' \
  --from-literal=KONX_LUCIE_NAME=Lucie
```

Et le secret `ghcr-pull` (pull GHCR) dans le namespace, comme les autres apps.

## `KONX_AUTOBOOK` — règles d'auto-réservation

JSON, une entrée par créneau récurrent à réserver automatiquement. `weekday` et
`time` sont ceux **du cours** ; l'ouverture est calculée à `cours − lead_hours`
(24 h par défaut → mardi 18:30 s'ouvre lundi 18:30).

```json
[
  { "user": "john",  "weekday": 1, "time": "18:30", "activity": "crossfit" },
  { "user": "lucie", "weekday": 1, "time": "18:30", "activity": "crossfit" },
  { "user": "john",  "weekday": 3, "time": "18:30", "activity": "crossfit" },
  { "user": "lucie", "weekday": 6, "time": "10:00", "activity": "natation" }
]
```

`weekday` : 0 = lundi … 6 = dimanche. `activity` : `crossfit` | `natation`.

## Endpoints (internes, `konx-api.konx-api:8000`)

| Méthode | Route | Rôle |
|--------|-------|------|
| GET | `/health` | statut + comptes + nb de règles |
| GET | `/{user}/planning?d=YYYY-MM-DD` | cours du jour |
| POST | `/{user}/book?session_id=…&date=…` | réserver maintenant |
| DELETE | `/{user}/book?booking_id=…&session_id=…&date=…` | annuler |

## Déploiement

1. Pousser ce repo sur `main` → la CI ([.github/workflows/deploy.yml](.github/workflows/deploy.yml))
   build l'image, la pousse sur GHCR et bump le tag dans `argocd-apps`
   (secret repo `GITOPS_PAT` requis).
2. Créer les 3 secrets ci-dessus dans le cluster.
3. ArgoCD synchronise `apps/konx-api/` automatiquement.

## À finaliser au 1er déploiement (marqué `TODO(live)` dans `main.py`)

1. **Correctif d'une ligne** — `auth_cookie()` : ajouter `from urllib.parse import quote`
   en tête, et terminer par
   `return f"{AUTH_COOKIE}={quote(json.dumps(session, separators=(',', ':')))}"`.
2. **Format du cookie `sb-…-auth-token`** attendu par le serveur KONX
   (JSON encodé URL vs préfixe `base64-`, découpage `.0/.1`) — à confirmer sur
   une vraie requête authentifiée.
3. **`_parse_planning()`** — extraire `session_id` / `activity` / `start_time`
   du payload RSC de `/app/planning?d=…` (le sniper en a besoin pour cibler le
   cours du jour).

Ces trois points se règlent en observant les logs d'un `book` réel après
déploiement.
