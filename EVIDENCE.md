# Evidence

Each Requirements checkbox has one pasted proof. Claims without evidence score as not done — command output is pasted as-is.

## Widget management & tenant isolation

**Unauthenticated requests are rejected (401, not 500):**
```
no token GET /widgets          -> 401
no token GET /dashboard/1/stats -> 401
```
`HTTPBearer(auto_error=False)` previously let a missing header fall through to `credentials.credentials` on `None` → 500. Now guarded up front.

**Multi-tenant isolation — live PowerShell transcript (2026-09-18):**

Tenants already existed, so this run used password sign-in (`POST /auth/v1/token?grant_type=password`) rather than signup. The `apikey` header is redacted here; the real key lives in `.env` only.

```
PS> $responseA = Invoke-WebRequest -UseBasicParsing -Uri ".../auth/v1/token?grant_type=password" -Method POST -Headers @{apikey="REDACTED"; "Content-Type"="application/json"} -Body '{"email":"tenant-a@example.com","password":"password123"}'
PS> $tokenA = ($responseA.Content | ConvertFrom-Json).access_token

PS> $responseB = Invoke-WebRequest -UseBasicParsing -Uri ".../auth/v1/token?grant_type=password" -Method POST -Headers @{apikey="REDACTED"; "Content-Type"="application/json"} -Body '{"email":"tenant-b@example.com","password":"password123"}'
PS> $tokenB = ($responseB.Content | ConvertFrom-Json).access_token

PS> $createResponse = Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:8003/widgets -Method POST -Headers @{Authorization="Bearer $tokenA"} -Body '{"type":"signup_form","title":"Tenant A Widget","fields":[{"name":"email","type":"email","label":"Email","required":true}]}' -ContentType "application/json"
PS> $createResponse.Content
{"id":9,"title":"Tenant A Widget","type":"signup_form","embed_snippet":"<script src=\"http://127.0.0.1:8003/widget.js?id=9\"></script>"}

PS> $widgetIdA = ($createResponse.Content | ConvertFrom-Json).id
PS> Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8003/widgets/$widgetIdA" -Headers @{Authorization="Bearer $tokenB"}
Invoke-WebRequest : {"detail":"Widget 9 not found"}
    + CategoryInfo          : InvalidOperation: (System.Net.HttpWebRequest:HttpWebRequest) [Invoke-WebRequest], WebException
    + FullyQualifiedErrorId : WebCmdletWebResponseException,Microsoft.PowerShell.Commands.InvokeWebRequestCommand

PS> Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:8003/widgets -Headers @{Authorization="Bearer $tokenB"}

StatusCode        : 200
StatusDescription : OK
Content           : []
RawContent        : HTTP/1.1 200 OK
                    Content-Length: 2
                    Content-Type: application/json
                    Date: Fri, 18 Sep 2026 03:35:01 GMT
                    Server: uvicorn

                    []
```

Tenant B cannot GET tenant A's widget (`404`, not the row) and B's list is empty (`[]`). Isolation is in SQL (`WHERE id = %s AND tenant_id = %s`), not in the UI.

**Same session, remaining isolation verbs** (`scripts/isolation_proof.ps1` — tenant B vs widget 9):

```
B PUT  /widgets/9            -> 404 {"detail":"Widget 9 not found"}
B DELETE /widgets/9          -> 404 {"detail":"Widget 9 not found"}
B GET  /dashboard/9/stats    -> 404 {"detail":"Widget 9 not found"}
A GET  /dashboard/9/stats    -> 200 {"widget_id":9,"total_submissions":0,"by_day":[],"by_country":[]}
public POST /submissions (no token)   -> 201 {"id":36,...,"geo":{"country":"Testland","city":"Localhost"},"deduplicated":false}
A GET  /dashboard/9/stats after submit -> 200 {"widget_id":9,"total_submissions":1,"by_day":[{"day":"2026-09-18","count":1}],"by_country":[{"country":"Testland","count":1}]}
```

## Widget delivery & cross-origin rendering

**Widget renders on a second-origin page (Phase 3 gate):**
Customer site served at `http://127.0.0.1:5500` (plain HTML via `python -m http.server`), loading `<script src="http://127.0.0.1:8003/widget.js?id=1">` — distinct origin from the API. The widget rendered, and submitting returned "Thanks! Your submission was received." Full chain proven: config fetch → render → cross-origin POST → success.

**Cache headers verified:**
```
GET /widgets/1/config    -> Cache-Control: public, max-age=300
GET /widget.js?v=1       -> Cache-Control: public, max-age=31536000, immutable
```

**Embed snippet generated per widget** (from `POST /widgets`, also on `GET /widgets` and `GET /widgets/{id}`):
```
-> A's widget id=8 embed=<script src="http://127.0.0.1:8003/widget.js?id=8"></script>
```

## Public submission API (cross-origin, validated)

**Cross-origin submission stores an enriched row:**
```
POST /submissions  (Origin: http://127.0.0.1:5500)  -> 201
{"id":1,"created_at":"...","geo":{"country":"Testland","city":"Localhost"}}
access-control-allow-origin: *    <- response header, readable by any browser
```
(`127.0.0.1` maps to the deterministic fake geo "Testland"/"Localhost" so local testing never resembles a provider failure — real public IPs resolve through the real providers.)

**Malformed payload rejected with clean 4xx JSON, never a 500:**
```
POST /submissions {"widget_id": "not-a-number", ...}
-> 422 {"detail":[{"type":"int_parsing","loc":["body","widget_id"],"msg":"Input should be a valid
      integer, unable to parse string as an integer",...}]}
```

**Unknown field rejected:**
```
POST /submissions {"widget_id":1,"data":{"phone":"123"}}   -> 400 {"detail":"Unknown fields: ['phone']"}
```

**Oversized payload rejected with 413:**
```
POST /submissions  60,028-byte JSON body
-> 413 {"detail":"Request body too large"}      <- Content-Length middleware (50 KB cap)
```

## Abuse protection

**Rate limiting per-IP + per-widget (5 req / 10 s) → 429, and the API recovers:**
```
burst req 1: 201  burst req 2: 201  burst req 3: 201  burst req 4: 201  burst req 5: 201  burst req 6: 429
waiting 11s for window to slide...
request after window: 201
```
A flood gets `429`s while legitimate traffic (after the window) still succeeds.

**Honeypot spam control — submission silently dropped:**
```
POST /submissions {"widget_id":1,"data":{"email":"bot@x.com"},"website":"http://spam.example"}
-> 200 {"status":"ok","spam_flag":true}
```
Bot learns nothing; nothing is stored (verified by row count). The hidden `website` field is rendered by the widget JS and never exposed to real visitors (it is `position:absolute; left:-9999px`, `tabindex=-1`).

## Idempotency

**Retried submission with the same `idempotency_key` is stored once:**
```
idem id1=26 id2=26 same=True dedup1=False dedup2=True
```
First POST returns `"deduplicated": false`; the retry returns the *same row* with `"deduplicated": true` (DB unique index on `(tenant_id, idempotency_key)` + `ON CONFLICT DO NOTHING`).

## Geo enrichment fallback chain

**Deterministic mocked proof** (`test_geo_fallback.py`, per the brief's instruction to mock for this proof):
```
Provider A succeeds  -> used directly:        {'country': 'MockCountryA', 'city': 'MockCityA'}
Provider A fails, B succeeds -> fallback used: {'country': 'MockCountryB', 'city': 'MockCityB'}
Both providers fail  -> graceful degradation: {'country': None, 'city': None}
All fallback chain scenarios passed.
```
Real-provider failure was also observed live during development (ipapi.co returned a genuine `429 RateLimited`); `enrich_ip()` absorbed it and returned `{country: None, city: None}` without raising — the submission succeeded, unenriched.

## Safe side effect (background, off the request path)

**Confirmation email (FastAPI `BackgroundTask`, 2 attempts) fails; submission already stored:**
```
[side-effect-failure] confirmation email attempt 1/2 failed, submission still stored: SMTP server unreachable (simulated failure for Phase 2 proof)
[side-effect-failure] confirmation email attempt 2/2 failed, submission still stored: SMTP server unreachable (simulated failure for Phase 2 proof)
[side-effect-alert] confirmation email failed after all attempts
INFO: 127.0.0.1:xxxxx - "POST /submissions HTTP/1.1" 201 Created
```
The task runs after the response is sent and never touches the stored row — a dead SMTP server cannot fail a submission.

## Dashboard stats

Authenticated, tenant-scoped, aggregated:
```
A GET /dashboard/8/stats -> 200 {"widget_id":8,"total_submissions":0,"by_day":[],"by_country":[]}
```
After a public submission, counts update; the full /dashboard output for widget 1 (early dev):
```
{"widget_id":1,"total_submissions":16,"by_day":[{"day":"2026-09-16","count":2},{"day":"2026-09-15","count":14}],"by_country":[{"country":"Testland","count":16}]}
```

## Migrations persist (schema as migrations)

```
python -m migrate
applied 0001_initial.sql
applied 0002_idempotency.sql
schema_migrations: ['0001_initial.sql', '0002_idempotency.sql']
idempotency_key column: True
unique index: True
```
Note: the migration runner had a real bug — the applied-set `SELECT` left an implicit open transaction so `conn.transaction()` only opened a savepoint and `close()` rolled everything back. Fixed in `migrate.py` (explicit `conn.commit()` after the loop) and verified above.