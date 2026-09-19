# Embeddable Widget & Lead-Capture Platform

A platform for creating embeddable form widgets, installed on any website with a single `<script>` tag. Submissions arrive from the open internet — validated at the boundary, rate-limited, spam-filtered, enriched with geo data, stored, and viewable in a tenant-isolated dashboard.

Built for the FlyRank Backend Track capstone. Stack: FastAPI · PostgreSQL (Docker) · Supabase Auth · two free geo APIs.

## What it does

- **Widget management** — an authenticated owner creates signup/CTA/popover widgets with typed form fields. Widgets are tenant-isolated: every query filters by tenant, so one customer can never read or modify another's widgets or submissions.
- **Embed snippet** — the create/list/get responses return the one line to paste into any site:
  ```html
  <script src="http://127.0.0.1:8003/widget.js?id=1"></script>
  ```
- **Widget delivery** — a cached config endpoint (`Cache-Control: public, max-age=300`) and the embeddable JS bundle itself, served long-cached (`Cache-Control: public, max-age=31536000, immutable`).
- **Hardened submission API** — cross-origin `POST /submissions` with correct CORS (preflight handled), field-level validation (`400`/`422`), a body-size cap (`413`), per-IP **and** per-widget rate limiting (`429`), and a honeypot spam trap.
- **Enrichment + safe side effects** — the visitor's IP is geo-enriched through a provider fallback chain (ip-api.com → ipapi.co); if both are down the submission still stores, just without geo. A confirmation "email" runs off the request path as a FastAPI `BackgroundTask` with retries — a failure never breaks the stored submission.
- **Idempotency** — a retried submission with the same `idempotency_key` is deduplicated, not stored twice.
- **Owner dashboard** — authenticated, tenant-scoped stats: total submissions, counts per day, and a per-country breakdown.

## Architecture

```
Widget Owner (authenticated)                          Visitor (any website)
        │                                                  │
        ▼                                                  ▼
  Widget Management API ──► Widgets DB ──► embed snippet   POST /submissions  (public, CORS)
  (supabase Bearer token)         (tenant-isolated)        │  boundary validation → 4xx, never 500
                                                           │  rate limit (per-IP + per-widget) → 429
                                                           │  honeypot spam check → 200 spam_flag:true, dropped
                                                           │  geo enrichment: Provider A →(fails)→ B →(fails)→ store anyway
                                                           │  store submission
                                                           │  background email side-effect (failure ≠ failure)
                                                           ▼
  Dashboard API (authenticated, tenant-scoped) ◄──────────  submissions + stats
```

## Quick start

Prerequisites: Python 3.10+, Docker, and a free Supabase project (for auth only — set `SUPABASE_URL`/`SUPABASE_KEY` in `.env`).

```bash
# 1. Copy env file and fill in your Supabase project credentials
cp .env.example .env

# 2. Create a virtualenv and install dependencies
python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Boot the database and the API (one command)
bash scripts/dev.sh             # -> API on http://127.0.0.1:8003, Postgres on :5433

# 4. In a second terminal — apply migrations + create the demo tenant/widget
python -m repository            # prints: Seeded tenant_id=..., widget_id=1

# 5. (Optional) serve the "customer site" test page on a second origin
python -m http.server 5500 --directory customer-site    # -> http://127.0.0.1:5500
```

`dev.sh` is just `docker compose up -d db` followed by `python -m uvicorn main:app --host 127.0.0.1 --port 8003`. On Windows without Git Bash/WSL, run those two lines directly in PowerShell (`bash` itself is a sh script):

```powershell
docker compose up -d db
python -m uvicorn main:app --host 127.0.0.1 --port 8003
```

On the first boot uvicorn can start before Postgres is ready and exit with a connection error — just rerun the boot command; the container keeps running and migrations are idempotent.

On a fresh database the demo widget is `widget_id=1` ("Test Signup Widget", one required email field). After other tenants/widgets exist (e.g. after `scripts/isolation_proof.ps1`) the id will be higher, so use the id printed by `python -m repository` in the test snippet below.

### Toggling the geo providers (for the fallback proof)

Edit `geo.py` and set `PROVIDER_A_ENABLED = False` (or `PROVIDER_B_ENABLED`) to deterministically exercise a provider being down. Deterministic tests: `python test_geo_fallback.py`.

## Tests (deterministic suite)

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Covers the brief's list — CORS preflight, invalid payloads (`400`/`422`/`413`), rate limiting (per-IP + per-widget with a sliding window), honeypot spam control, idempotency, and the geo provider-fallback chain. The suite stubs the SQL layer and geo/network providers in `conftest.py`, so it needs no live Postgres or Supabase network access and its results are independent of DB seed state (works on a clean machine right after `pip install`).

## API documentation

Authorized endpoints require `Authorization: Bearer <supabase-access-token>`.

### Path 1 — Widget owner (authenticated)

| Method | Path | Description |
|---|---|---|
| `POST` | `/widgets` | Create a widget. Body: `{type, title, fields:[{name,label,type,required}], button_text?}`. `type` ∈ `signup_form\|cta\|popover`. Returns `id`, `title`, `type`, `embed_snippet` → `201` |
| `GET` | `/widgets` | List the caller's widgets (with `embed_snippet`) |
| `GET` | `/widgets/{id}` | Get one widget; `404` if it belongs to another tenant |
| `PUT` | `/widgets/{id}` | Update `title`/`button_text` (bumps `bundle_version`) |
| `DELETE` | `/widgets/{id}` | Delete a widget → `204` |
| `GET` | `/dashboard/{widget_id}/stats` | `total_submissions`, `by_day` (7 days), `by_country` |

### Path 2 — Customer website (public, cached)

| Method | Path | Description |
|---|---|---|
| `GET` | `/widgets/{id}/config` | Public config, `Cache-Control: public, max-age=300` |
| `GET` | `/widget.js` | The embeddable bundle (loaded as `widget.js?id=N`), `Cache-Control: public, max-age=31536000, immutable` |

### Path 3 — Website visitor (public, hardened)

`POST /submissions` — body: `{widget_id, data: {field: value}, website?, idempotency_key?}`.

| Status | Meaning |
|---|---|
| `201` | Stored (and once — retried `idempotency_key`s return the same row, flagged `"deduplicated": true`) |
| `200` | `website` honeypot filled — accepted-looking response with `"spam_flag": true`; no row is stored |
| `400` / `422` | Malformed payload, unknown/missing fields, too many/too-long fields |
| `404` | (config only) unknown widget |
| `413` | Request body larger than 50 KB |
| `429` | Rate limit (5 req/10 s) per-IP or per-widget |

Example:

```bash
curl -X POST http://127.0.0.1:8003/submissions \
  -H "Content-Type: application/json" \
  -H "Origin: http://127.0.0.1:5500" \
  -d '{"widget_id": 1, "data": {"email": "visitor@example.com"}}'
```

## Multi-tenant isolation proof

`scripts/isolation_proof.ps1` signs up — or signs in, if already registered — two Supabase users (tenant A and tenant B) and asserts: no-token requests are rejected (`401`), and B cannot list, read, update, or delete A's widgets, nor view A's dashboard stats. It needs real Supabase credentials in `.env`. Output is recorded in `EVIDENCE.md`.

## Repository layout

```
main.py          FastAPI routes (HTTP layer)
repository.py    All SQL — migrations-driven, tenant-scoped queries (data layer)
auth.py          Supabase auth helper (tenant resolution)
geo.py           IP→geo with provider fallback chain
migrate.py       Applies migrations/ in order (tracked in schema_migrations)
migrations/      0001_initial.sql, 0002_idempotency.sql
static/widget.js The embeddable bundle the <script> tag loads
customer-site/   Plain HTML "customer site" on a second origin (port 5500)
scripts/         dev.sh (one-command boot), isolation_proof.ps1
conftest.py      Pytest bootstrap — imports the app without a live DB (no-op migrations)
test_cors.py     Deterministic CORS preflight tests
test_submissions.py  Deterministic payload / rate-limit / spam / idempotency tests
test_geo_fallback.py   Deterministic fallback-chain tests (mocked providers)
requirements-dev.txt   Test-only deps (pytest, httpx — installed by the test command above)
```

## Known limitations (honest)

- **Auth requires Supabase** — a free project URL + anon key must be in `.env`. It's not optional at boot: `main.py` imports `auth.py` at startup, which reads `SUPABASE_URL`/`SUPABASE_KEY` eagerly, so the API won't start without them — even for the public `POST /submissions`.
- **Rate limiter is in-process memory** — correct for a single instance / proof; a real deploy would move it to Redis. It also resets on restart.
- **Email is simulated** — `send_confirmation_email` deliberately raises so the fail-safe path is provable. Swap in a real SMTP client (or Mailpit) in `main.py:send_confirmation_email`.
- **Geo enrichment needs a real, public IP** — `127.0.0.1`/localhost map to a fake "Testland"/"Localhost" row so local testing is deterministic; free-tier providers are rate-limited (ip-api.com 45 req/min, ipapi.co ~1,000/day), which is exactly what the fallback chain absorbs.
- **The widget bundle is immutable-cached but not content-addressed** — the embed loads `widget.js?id=N`, and `/widget.js` accepts a `?v=` query param it never uses, so the JS is cached effectively forever once a browser sees it. A real fix would serve a content-hashed bundle (e.g. per content hash) and bump the config-url accordingly.
- **No real front-end dashboard** — this is a backend capstone; the dashboard is a REST API.