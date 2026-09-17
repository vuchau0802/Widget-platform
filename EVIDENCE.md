# Evidence

## Widget delivery / submission core

**Cross-origin submission stores an enriched row (Phase 2 gate):**
```
PS> Invoke-WebRequest -UseBasicParsing -Uri http://localhost:8003/submissions -Method POST -Body '{"widget_id": 1, "data": {"email": "visitor@example.com"}}' -ContentType "application/json" -Headers @{Origin="http://some-other-site.com"}


StatusCode        : 201
StatusDescription : Created
Content           : {"id":1,"created_at":"2026-09-15T02:38:33.424039+00:00","spam_flag":false,"geo":{"country":"Testlan
                    d","city":"Localhost"}}
RawContent        : HTTP/1.1 201 Created
                    access-control-allow-origin: *
                    Content-Length: 122
                    Content-Type: application/json
                    Date: Tue, 15 Sep 2026 02:38:33 GMT
                    Server: uvicorn

                    {"id":1,"created_at":"2026-09-15T02:3...
Forms             :
Headers           : {[access-control-allow-origin, *], [Content-Length, 122], [Content-Type, application/json], [Date,
                    Tue, 15 Sep 2026 02:38:33 GMT]...}
Images            : {}
InputFields       : {}
Links             : {}
ParsedHtml        :
RawContentLength  : 122
```
The `access-control-allow-origin: *` response header confirms a real browser on any origin would be permitted to read this response — not just that the server-side call succeeded.

## Abuse protection

**Rate limiting (5 requests/10s window):**
```
6 rapid requests fired. Requests 1-5: 201 Created. Request 6: 429 {"detail":"Too many requests. Please slow down."}

Invoke-WebRequest : {"detail":"Too many requests. Please slow down."}
At line:1 char:25
+ ... ch-Object { Invoke-WebRequest -UseBasicParsing -Uri http://localhost: ...
+                 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : InvalidOperation: (System.Net.HttpWebRequest:HttpWebRequest) [Invoke-WebRequest], WebExc
   eption
    + FullyQualifiedErrorId : WebCmdletWebResponseException,Microsoft.PowerShell.Commands.InvokeWebRequestCommand
```

**Honeypot spam control — submission silently dropped, not stored:**

```
$ curl --data-binary "@honeypot.json" http://127.0.0.1:8003/submissions
{"status":"ok","spam_flag":true}
HTTP 200
$ after  = SELECT count(*) FROM submissions   -> 18
```

A filled honeypot returns `200 {"status":"ok","spam_flag":true}` (so the bot learns nothing) but **nothing is stored** — the count is unchanged. Per the brief: "silently dropped or rejected."

## Input validation

**Malformed payload (non-numeric widget_id):**
```
PS> Invoke-WebRequest -UseBasicParsing -Uri http://localhost:8003/submissions -Method POST -Body '{"widget_id": "not-a-number", "data": {}}' -ContentType "application/json"
Invoke-WebRequest : {"detail":[{"type":"int_parsing","loc":["body","widget_id"],"msg":"Input should be a valid
integer, unable to parse string as an integer","input":"not-a-number"}]}
At line:1 char:1
+ Invoke-WebRequest -UseBasicParsing -Uri http://localhost:8003/submiss ...
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : InvalidOperation: (System.Net.HttpWebRequest:HttpWebRequest) [Invoke-WebRequest], WebExc
   eption
    + FullyQualifiedErrorId : WebCmdletWebResponseException,Microsoft.PowerShell.Commands.InvokeWebRequestCommand
```
Rejected with a clean 4xx and a JSON error — never a 500.

## Geo enrichment fallback chain

**Real provider failure observed during manual testing:**
```
PS> python -c "import requests; r = requests.get('https://ipapi.co/8.8.8.8/json/', timeout=5); print(r.status_code); print(r.text)"
429
{"reason": "RateLimited", "message": "Please sign up for a paid plan at https://ipapi.co/pricing or contact us for a trial account", "wait": 1.0, "error": true}
```
`ipapi.co`'s free tier was genuinely rate-limited during development testing — a real instance of the exact failure this system is designed to survive. `enrich_ip()` correctly caught this and returned `{'country': None, 'city': None}` without crashing.

**Deterministic mocked proof** (per the brief's instruction to mock providers for this specific proof), `test_geo_fallback.py`:
```
Provider A succeeds -> used directly: {'country': 'MockCountryA', 'city': 'MockCityA'}
Provider A fails, B succeeds -> fallback used: {'country': 'MockCountryB', 'city': 'MockCityB'}
Both providers fail -> graceful degradation, no crash: {'country': None, 'city': None}
All fallback chain scenarios passed.
```

## Safe side effect

**Confirmation email deliberately fails; submission still succeeds:**
```
[side-effect-failure] confirmation email failed, submission still stored: SMTP server unreachable (simulated failure for Phase 2 proof)
INFO: 127.0.0.1:53643 - "POST /submissions HTTP/1.1" 201 Created
```
The failure log appears immediately before the successful `201` response on every request — proving the side effect's failure never blocks or reverts the already-stored submission.

## Widget delivery & cross-origin rendering (Phase 3)
 
**Widget renders on a second-origin page (Phase 3 gate):**
 
Customer site served at `http://127.0.0.1:5500` (plain HTML, `python -m http.server`), loading `<script src="http://127.0.0.1:8003/widget.js?id=1">`. The API binds `127.0.0.1` only, so the test page uses the explicit IPv4 address rather than `localhost` — which Chrome resolves to `::1` first and gets `ERR_CONNECTION_REFUSED` against an IPv4-only server. Ports 5500 vs 8003 are distinct origins, so this is a genuine cross-origin test, not a same-origin coincidence.
 
Result: the widget rendered correctly ("Test Signup Widget" title, Email input, Submit button), and submitting the form returned "Thanks! Your submission was received." — proving the full chain (config fetch → render → cross-origin POST → success) works end to end from a page that shares no code with the API.
 
**Cache headers verified:**
```
GET /widgets/1/config    -> Cache-Control: public, max-age=300
GET /widget.js?v=1       -> Cache-Control: public, max-age=31536000, immutable
```
 
**Dashboard stats:**
```json
{"widget_id":1,"total_submissions":16,"by_day":[{"day":"2026-09-16","count":2},{"day":"2026-09-15","count":14}],"by_country":[{"country":"Testland","count":16}]}
```
 
## Known gap (honest, tracked for future work)
 
`GET /dashboard/{widget_id}/stats` is currently unauthenticated. The brief specifies this should be a tenant-scoped, authenticated endpoint (same pattern as BE-03's Supabase auth). This is the next item to add before multi-tenant isolation can be considered fully proven — tracked here rather than silently left out.