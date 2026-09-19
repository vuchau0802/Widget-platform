# Build Log

Honesty, not perfection. I used an AI assistant as a helper (lookups, first-pass boilerplate, formatting). The design, the attack tests, the bug fixes, and the proofs in `EVIDENCE.md` are mine. I can explain any of the request path by hand.

No paid APIs. Postgres is Docker. Geo is the two free providers. Auth is a free Supabase project. Assistant usage is my own subscription — nothing in this repo is metered, so there is no per-call budget guard.

---

## 2026-09-18 — Isolation proof

I signed in tenant A and tenant B in PowerShell (`grant_type=password`), created a widget as A (`id=9`), then called the API as B.

- `GET /widgets/9` as B → `404 {"detail":"Widget 9 not found"}`
- `GET /widgets` as B → `200 []`

I pasted that transcript into `EVIDENCE.md` and redacted the Supabase `apikey`. Tokens stay out of the repo.

Assistant: only helped format the paste. I ran the commands.

---

## 2026-09-17 — Hardening after reading the brief again

I went back through Sections 6, 11, and 13 against my own code. Several gaps were mine from earlier shortcuts (copying a FastAPI auth pattern without a null check, shipping dashboard stats before I added the Bearer dependency, leaving idempotency in SQL but not on the route). I fixed them in `main.py` / `repository.py` / `auth.py` and re-proved isolation.

What I changed, and why:

| Gap I found | Why it failed the brief | What I did |
|---|---|---|
| Missing `Authorization` crashed with 500 (`credentials` was `None`) | Unauthenticated requests must be rejected, not 500 | Guard in `get_current_tenant_id` → `401` |
| Dashboard stats had no auth / no tenant filter | Tenant A must not read B's submissions | Bearer + `WHERE tenant_id = %s` on every stats query |
| Create-widget response had no snippet | "Embed snippet generated per widget" | Return `embed_snippet` on create/list/get, from `PUBLIC_BASE_URL` |
| Confirmation email ran on the request path | Shared req: background job, retries, failure alert | `BackgroundTasks`, 2 attempts, `[side-effect-alert]` log |
| `idempotency_key` lived in the DB layer only | Retried action must happen once | Accept it on `POST /submissions`; unique `(tenant_id, idempotency_key)` |
| Rate limit was per-IP only; no body cap | Per-IP and per-widget; oversized → 4xx | Second limiter key `widget:{id}` + 50 KB `413` middleware |
| Widget `type` / fields were loose | Brief names signup / CTA / popover | Enum + field shape validation |

Calls I made (not the assistant):

- Honeypot stays `200 {"status":"ok","spam_flag":true}` — look successful, store nothing. (I know that is not fully silent; I chose not to 4xx so a bot does not learn the trap.)
- Body cap 50 KB, email retries 2 — documented in README.
- Migration runner: the applied-set `SELECT` left an open transaction, so `close()` rolled the inserts back. I added an explicit `conn.commit()` in `migrate.py` and re-ran `python -m migrate`.

Assistant: I asked it to double-check the brief checkboxes and to draft `capstone.yaml` / README structure after I already had the API working. I did not accept a blind rewrite of the app.

---

## Phase 3 — widget delivery (2026-09-16)

I added the public config route (`Cache-Control: public, max-age=300`), the long-cached `widget.js` bundle, dashboard aggregates, and `customer-site/` on port 5500.

I served the HTML myself (`python -m http.server 5500`), loaded `widget.js?id=1` from `:8003`, and confirmed the form rendered and submitted. Cache headers are in `EVIDENCE.md`.

Versioning is query-param `?v=`, not a content hash. I left that as a README limitation instead of pretending the URL is immutable-per-build.

Assistant: boilerplate for the `FileResponse` headers. I wired `bundle_version` bump on update and wrote the test page.

---

## Phase 2 — hardened submission path (2026-09-14)

I built `POST /submissions` as the public path: CORS, Pydantic shape checks, field checks against the widget schema, in-memory rate limit, honeypot field `website`, geo fallback in `geo.py` (ip-api.com → ipapi.co → store anyway), and an email side effect that is allowed to fail.

I attacked it: burst until `429`, then a request after the window; malformed `widget_id`; unknown fields; 60 KB body; honeypot filled. Outputs are in `EVIDENCE.md`.

Local IPs (`127.0.0.1`) never hit a real geo API, so I short-circuit them to `Testland` / `Localhost` — otherwise every local 201 looks like a provider outage. Fallback itself is proven with mocks in `test_geo_fallback.py` (what the brief asks for). I also saw a real `429` from ipapi.co during manual testing; `enrich_ip()` swallowed it.

Assistant: I used docs-style snippets for FastAPI CORS and Pydantic validators. The limiter keys, honeypot name, Testland shortcut, and the always-fail SMTP stub (so Probe 5 is deterministic) are my choices.

---

## Phase 1 — design (2026-09-13)

I wrote `design.md`: tenants / widgets / submissions, the snippet → config → render → submit flow, the three API paths, and one non-goal (no WebSockets / SSE).

I denormalized `tenant_id` onto submissions so dashboard queries stay tenant-scoped without a join, and added `bundle_version` on widgets for cache-busting.

Assistant: I had it list the brief's required files so I did not miss `capstone.yaml` / `.env.example`. The data-model choices and the non-goal are mine.
