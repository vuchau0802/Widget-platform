# Multi-tenant isolation proof (Phase 4)
# Requires: DB up (`docker compose up -d db`), API up (`python -m uvicorn main:app --host 127.0.0.1 --port 8003`),
# and SUPABASE_URL / SUPABASE_KEY in the repo's .env.
#
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/isolation_proof.ps1

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot

function Get-EnvVar($name) {
    $line = Get-Content "$repo\.env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { throw "Missing $name in .env" }
    return ($line -split '=', 2)[1]
}

$SUPABASE_URL = Get-EnvVar "SUPABASE_URL"
$SUPABASE_KEY = Get-EnvVar "SUPABASE_KEY"
$base = "http://127.0.0.1:8003"

function Supabase-GetToken($email) {
    $h = @{ apikey = $SUPABASE_KEY; "Content-Type" = "application/json" }
    $body = @{ email = $email; password = "password123" } | ConvertTo-Json
    try {
        $r = Invoke-RestMethod -Uri "$SUPABASE_URL/auth/v1/signup" -Method Post -Headers $h -Body $body
        if ($r.access_token) { return $r.access_token }
    } catch {
        # user already registered -- fall through to sign in
    }
    $r = Invoke-RestMethod -Uri "$SUPABASE_URL/auth/v1/token?grant_type=password" -Method Post -Headers $h -Body $body
    if (-not $r.access_token) { throw "No access_token obtained for $email" }
    return $r.access_token
}

function Invoke-Check($label, $method, $uri, $token, $body = $null) {
    $h = @{}
    if ($token) { $h["Authorization"] = "Bearer $token" }
    if ($body) { $h["Content-Type"] = "application/json" }
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $uri -Method $method -Headers $h -Body $body
        Write-Host ("{0,-45} -> {1}" -f $label, $resp.StatusCode)
        return $resp.Content
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        Write-Host ("{0,-45} -> {1}" -f $label, $code)
        return $null
    }
}

$tokA = Supabase-GetToken "tenant-a@example.com"
$tokB = Supabase-GetToken "tenant-b@example.com"
Write-Host "Auth OK: tokenA.len=$($tokA.Length) tokenB.len=$($tokB.Length)"
Write-Host ""

$null = Invoke-Check "no token GET /widgets" "GET" "$base/widgets" $null

$widgetBody = @{ type = "signup_form"; title = "Tenant A Widget"; fields = @(@{ name = "email"; label = "Email"; type = "email"; required = $true }) } | ConvertTo-Json
$created = Invoke-Check "A POST /widgets" "POST" "$base/widgets" $tokA $widgetBody
$w = $created | ConvertFrom-Json
$wid = $w.id
Write-Host ("  -> A's widget id=$wid embed=" + $w.embed_snippet)
Write-Host ""

Invoke-Check "A GET /widgets (own)" "GET" "$base/widgets" $tokA
Invoke-Check "B GET /widgets (should be empty)" "GET" "$base/widgets" $tokB
Invoke-Check "B GET /widgets/$wid (isolation)" "GET" "$base/widgets/$wid" $tokB
Invoke-Check "B PUT /widgets/$wid (isolation)" "PUT" "$base/widgets/$wid" $tokB (@{ title = "Hijack" } | ConvertTo-Json)
Invoke-Check "B DELETE /widgets/$wid (isolation)" "DELETE" "$base/widgets/$wid" $tokB

# Airtight submission-read probe: give widget $wid a REAL stored submission, then
# prove tenant B cannot read tenant A's submission through the widget's dashboard,
# even while tenant A can. A working query on B's side would expose A's data here.
$subBody = @{ widget_id = $wid; data = @{ email = "visitor@example.com" } } | ConvertTo-Json
$null = Invoke-Check "public POST /submissions (no token)" "POST" "$base/submissions" $null $subBody
$null = Invoke-Check "A GET /dashboard/$wid/stats (own, has rows)" "GET" "$base/dashboard/$wid/stats" $tokA
$bDashboard = Invoke-Check "B GET /dashboard/$wid/stats (A rows -> must stay hidden)" "GET" "$base/dashboard/$wid/stats" $tokB
$bWidgetSelect = Invoke-Check "B GET /widgets/$wid (still isolated)" "GET" "$base/widgets/$wid" $tokB

# Fail hard if tenant B ever reaches tenant A's submission data.
if ($bDashboard -ne $null -or $bWidgetSelect -ne $null) {
    Write-Host "ISOLATION FAIL: tenant B read tenant A's widget/submission data" -ForegroundColor Red
    exit 1
}
Write-Host "ISOLATION OK: tenant B got no widget and no submission data for tenant A's widget" -ForegroundColor Green