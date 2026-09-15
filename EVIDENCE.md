# Evidence

## Widget delivery / submission core

**Cross-origin submission stores an enriched row (Phase 2 gate):**
```
PS> Invoke-WebRequest -Uri http://localhost:8003/submissions -Method POST `
    -Body '{"widget_id": 1, "data": {"email": "visitor@example.com"}}' `
    -ContentType "application/json" -Headers @{Origin="http://some-other-site.com"}

StatusCode: 201
Headers: access-control-allow-origin: *
Content: {"id":1,"created_at":"2026-09-15T02:38:33...","spam_flag":false,"geo":{"country":"Testland","city":"Localhost"}}
```
The `access-control-allow-origin: *` response header confirms a real browser on any origin would be permitted to read this response — not just that the server-side call succeeded.

## Abuse protection

**Rate limiting (5 requests/10s window):**
```
6 rapid requests fired. Requests 1-5: 201 Created. Request 6: 429 {"detail":"Too many requests. Please slow down."}
```

**Honeypot spam control:**
```
PS> ... -Body '{"widget_id": 1, "data": {"email": "bot@spam.com"}, "website": "http://spam.com"}'
StatusCode: 201
Content: {"id":7, ..., "spam_flag":true, ...}
```
The submission was accepted (not rejected, per the brief's "silently dropped or rejected") but correctly flagged as spam via the filled honeypot field.

## Input validation

**Malformed payload (non-numeric widget_id):**
```
PS> ... -Body '{"widget_id": "not-a-number", "data": {}}'
StatusCode: 422
Content: {"detail":[{"type":"int_parsing","loc":["body","widget_id"],"msg":"Input should be a valid integer..."}]}
```
Rejected with a clean 4xx and a JSON error — never a 500.

## Geo enrichment fallback chain

**Real provider failure observed during manual testing:**
```
PS> python -c "import requests; r = requests.get('https://ipapi.co/8.8.8.8/json/', timeout=5); print(r.status_code); print(r.text)"
429
{"reason": "RateLimited", "message": "Please sign up for a paid plan...", "error": true}
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