import os
import time
import json
import secrets
import datetime
import asyncio
from pathlib import Path
from collections import defaultdict, deque
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel, field_validator
from repository import (
    init_db, get_widget_by_id, insert_submission, get_db,
    get_or_create_tenant, create_widget, list_widgets_for_tenant,
    get_widget_for_tenant, update_widget_for_tenant, delete_widget_for_tenant,
    confirm_submission, get_submissions_for_export, soft_delete_by_email,
)
from geo import enrich_ip
from auth import supabase
from event_bus import bus
import bot

app = FastAPI()
security_scheme = HTTPBearer(auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

init_db()

MAX_PAYLOAD_FIELDS = 20
MAX_FIELD_LENGTH = 2000
MAX_BODY_BYTES = 50_000
WIDGET_TYPES = {"signup_form", "cta", "popover"}
FIELD_TYPES = {"text", "email", "tel", "number", "url"}

RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 10
request_log: dict[str, deque] = defaultdict(deque)

POW_DIFFICULTY_BITS = int(os.environ.get("POW_DIFFICULTY_BITS", "5"))
BOT_REJECTED = 0
BOT_ACCEPTED = 0

STATIC_DIR = Path(__file__).parent / "static"


def _load_widget_version() -> str:
    """Load the content hash from the build script output."""
    ver_file = STATIC_DIR / "widget.version.txt"
    if ver_file.exists():
        return ver_file.read_text().strip()
    return "dev"


WIDGET_VERSION = _load_widget_version()


def build_embed_snippet(widget_id: int) -> str:
    """The one line a customer pastes into their site. The public base URL is
    configurable via PUBLIC_BASE_URL so a real deployment points at its own API.
    Includes ?v= for cache-busting on bundle updates."""
    base = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
    return f'<script src="{base}/widget.js?id={widget_id}&v={WIDGET_VERSION}"></script>'


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
            headers={"access-control-allow-origin": "*"},
        )
    return await call_next(request)


def is_rate_limited(key: str) -> bool:
    now = time.time()
    window = request_log[key]
    while window and window[0] < now - RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    window.append(now)
    return False


class SubmissionPayload(BaseModel):
    widget_id: int
    data: dict[str, str]
    website: str = ""
    idempotency_key: str | None = None
    proof: str = ""
    consent_given: bool = False

    @field_validator("proof")
    @classmethod
    def validate_proof(cls, value: str) -> str:
        if len(value) > 128:
            raise ValueError("proof too long (max 128)")
        return value

    @field_validator("data")
    @classmethod
    def validate_data_shape(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_PAYLOAD_FIELDS:
            raise ValueError(f"too many fields (max {MAX_PAYLOAD_FIELDS})")
        for key, val in value.items():
            if len(key) > 64:
                raise ValueError(f"field name '{key}' exceeds max length (64)")
            if len(val) > MAX_FIELD_LENGTH:
                raise ValueError(f"field '{key}' exceeds max length ({MAX_FIELD_LENGTH})")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("idempotency_key cannot be empty")
            if len(value) > 128:
                raise ValueError("idempotency_key too long (max 128)")
        return value


def send_confirmation_email(data: dict, token: str) -> None:
    base = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
    confirm_url = f"{base}/confirm-email?token={token}"
    print(f"[double-opt-in] Confirmation URL: {confirm_url}")
    raise Exception("SMTP server unreachable (simulated failure for Phase 2 proof)")


def send_confirmation_email_safe(data: dict, token: str) -> None:
    """Runs in a FastAPI BackgroundTask (off the request path). Retries twice,
    then gives up — a failure must never break the already-stored submission."""
    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            send_confirmation_email(data, token)
            return
        except Exception as e:
            print(f"[side-effect-failure] confirmation email attempt {attempt}/{attempts} failed, "
                  f"submission still stored: {e}")
    print("[side-effect-alert] confirmation email failed after all attempts")


def validate_data_against_fields(data: dict, fields) -> None:
    allowed = {f["name"] for f in fields}
    required = {f["name"] for f in fields if f.get("required")}
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown fields: {sorted(unknown)}")
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {sorted(missing)}")


@app.get("/health")
def health():
    return {"status": "ok"}


def get_current_tenant_id(credentials=Depends(security_scheme)) -> int:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = credentials.credentials
    try:
        result = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if result is None or result.user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return get_or_create_tenant(result.user.id, result.user.email)


class WidgetCreate(BaseModel):
    type: str
    title: str
    fields: list[dict]
    button_text: str = "Submit"
    targeting_rules: dict | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in WIDGET_TYPES:
            raise ValueError(f"type must be one of {sorted(WIDGET_TYPES)}")
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, fields: list[dict]) -> list[dict]:
        if not fields:
            raise ValueError("at least one field is required")
        if len(fields) > MAX_PAYLOAD_FIELDS:
            raise ValueError(f"too many fields (max {MAX_PAYLOAD_FIELDS})")
        for field in fields:
            if not isinstance(field, dict):
                raise ValueError("each field must be an object")
            name = field.get("name")
            label = field.get("label")
            field_type = field.get("type", "text")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("each field needs a non-empty string 'name'")
            if len(name) > 64:
                raise ValueError("field name too long (max 64)")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"field '{name}' needs a non-empty string 'label'")
            if not isinstance(field_type, str) or field_type not in FIELD_TYPES:
                raise ValueError(f"field '{name}' has unsupported type '{field_type}'")
        return fields

    @field_validator("targeting_rules")
    @classmethod
    def validate_targeting_rules(cls, value: dict | None) -> dict | None:
        if value is None:
            return value
        allowed_keys = {"page_paths", "delay_seconds", "once_per_visitor"}
        unknown = set(value.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"unknown targeting rule keys: {sorted(unknown)}")
        if "page_paths" in value:
            if not isinstance(value["page_paths"], list):
                raise ValueError("page_paths must be a list of strings")
            for p in value["page_paths"]:
                if not isinstance(p, str) or not p.strip():
                    raise ValueError("each page_path must be a non-empty string")
        if "delay_seconds" in value:
            if not isinstance(value["delay_seconds"], (int, float)) or value["delay_seconds"] < 0:
                raise ValueError("delay_seconds must be a non-negative number")
        if "once_per_visitor" in value:
            if not isinstance(value["once_per_visitor"], bool):
                raise ValueError("once_per_visitor must be a boolean")
        return value


class WidgetUpdate(BaseModel):
    title: str | None = None
    button_text: str | None = None
    targeting_rules: dict | None = None

    @field_validator("title", "button_text")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("cannot be empty")
        if len(value) > 200:
            raise ValueError("too long (max 200)")
        return value

    @field_validator("targeting_rules")
    @classmethod
    def validate_targeting_rules(cls, value: dict | None) -> dict | None:
        if value is None:
            return value
        allowed_keys = {"page_paths", "delay_seconds", "once_per_visitor"}
        unknown = set(value.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"unknown targeting rule keys: {sorted(unknown)}")
        if "page_paths" in value:
            if not isinstance(value["page_paths"], list):
                raise ValueError("page_paths must be a list of strings")
            for p in value["page_paths"]:
                if not isinstance(p, str) or not p.strip():
                    raise ValueError("each page_path must be a non-empty string")
        if "delay_seconds" in value:
            if not isinstance(value["delay_seconds"], (int, float)) or value["delay_seconds"] < 0:
                raise ValueError("delay_seconds must be a non-negative number")
        if "once_per_visitor" in value:
            if not isinstance(value["once_per_visitor"], bool):
                raise ValueError("once_per_visitor must be a boolean")
        return value


@app.post("/widgets", status_code=201, summary="Create a widget (authenticated)")
def create_widget_route(payload: WidgetCreate, tenant_id: int = Depends(get_current_tenant_id)):
    widget = create_widget(tenant_id, payload.type, payload.title, payload.fields, payload.button_text,
                           payload.targeting_rules)
    return {
        "id": widget["id"],
        "title": widget["title"],
        "type": widget["type"],
        "embed_snippet": build_embed_snippet(widget["id"]),
    }


@app.get("/widgets", summary="List my widgets (authenticated)")
def list_widgets_route(tenant_id: int = Depends(get_current_tenant_id)):
    widgets = list_widgets_for_tenant(tenant_id)
    return [
        {
            "id": w["id"],
            "title": w["title"],
            "type": w["type"],
            "embed_snippet": build_embed_snippet(w["id"]),
        }
        for w in widgets
    ]


@app.get("/widgets/{widget_id}", summary="Get one of my widgets (authenticated, tenant-scoped)")
def get_widget_route(widget_id: int, tenant_id: int = Depends(get_current_tenant_id)):
    widget = get_widget_for_tenant(widget_id, tenant_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    result = dict(widget)
    result["embed_snippet"] = build_embed_snippet(widget["id"])
    return result


@app.put("/widgets/{widget_id}", summary="Update one of my widgets (authenticated, tenant-scoped)")
def update_widget_route(widget_id: int, payload: WidgetUpdate, tenant_id: int = Depends(get_current_tenant_id)):
    widget = update_widget_for_tenant(widget_id, tenant_id, payload.title, payload.button_text,
                                       payload.targeting_rules)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    return dict(widget)


@app.delete("/widgets/{widget_id}", status_code=204, summary="Delete one of my widgets (authenticated, tenant-scoped)")
def delete_widget_route(widget_id: int, tenant_id: int = Depends(get_current_tenant_id)):
    deleted = delete_widget_for_tenant(widget_id, tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")


# --- Phase 3: widget delivery ---

@app.get("/widgets/{widget_id}/config", summary="Public widget config, short-cached")
def get_widget_config(widget_id: int):
    widget = get_widget_by_id(widget_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")

    config = {
        "id": widget["id"],
        "type": widget["type"],
        "title": widget["title"],
        "fields": widget["fields"],
        "button_text": widget["button_text"],
        "bundle_version": widget["bundle_version"],
        "targeting_rules": widget.get("targeting_rules") or {},
    }
    return Response(
        content=json.dumps(config),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/widget.js", summary="The embeddable widget bundle, long-cached with content-hash versioning")
def get_widget_bundle(v: str = ""):
    min_file = STATIC_DIR / "widget.min.js"
    if min_file.exists():
        return FileResponse(
            str(min_file),
            media_type="application/javascript",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Widget-Version": WIDGET_VERSION,
            },
        )
    # Fallback to unminified if build hasn't run
    return FileResponse(
        str(STATIC_DIR / "widget.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/challenge", summary="Proof-of-work challenge for the embed (no-store)")
def get_bot_challenge(widget_id: int, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    return JSONResponse(
        content={
            "widget_id": widget_id,
            "challenge": bot.build_challenge(widget_id, client_ip, bot.now_window()),
            "difficulty": POW_DIFFICULTY_BITS,
            "window_seconds": bot.WINDOW_SECONDS,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/dashboard/{widget_id}/stats", summary="Aggregated submission stats for a widget (authenticated, tenant-scoped)")
def get_dashboard_stats(widget_id: int, tenant_id: int = Depends(get_current_tenant_id)):
    widget = get_widget_for_tenant(widget_id, tenant_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")

    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) AS count FROM submissions WHERE widget_id = %s AND tenant_id = %s",
        (widget_id, tenant_id),
    ).fetchone()["count"]

    by_day = conn.execute(
        """SELECT DATE(created_at) AS day, COUNT(*) AS count
           FROM submissions WHERE widget_id = %s AND tenant_id = %s
           GROUP BY DATE(created_at) ORDER BY day DESC LIMIT 7""",
        (widget_id, tenant_id),
    ).fetchall()

    by_geo = conn.execute(
        """SELECT COALESCE(country, 'Unknown') AS country, COUNT(*) AS count
           FROM submissions WHERE widget_id = %s AND tenant_id = %s
           GROUP BY country ORDER BY count DESC""",
        (widget_id, tenant_id),
    ).fetchall()

    conn.close()

    return {
        "widget_id": widget_id,
        "total_submissions": total,
        "by_day": [{"day": str(r["day"]), "count": r["count"]} for r in by_day],
        "by_country": [{"country": r["country"], "count": r["count"]} for r in by_geo],
    }


@app.get("/dashboard/{widget_id}/events", summary="SSE stream of new submissions (authenticated, tenant-scoped)")
async def dashboard_events(widget_id: int, request: Request, token: str = None,
                           credentials=Depends(security_scheme)):
    resolved_token = token or (credentials.credentials if credentials else None)
    if not resolved_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        result = supabase.auth.get_user(resolved_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if result is None or result.user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant_id = get_or_create_tenant(result.user.id, result.user.email)

    widget = get_widget_for_tenant(widget_id, tenant_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")

    queue = bus.subscribe(widget_id)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {"event": "new_submission", "data": data}
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "ping"}
        finally:
            bus.unsubscribe(widget_id, queue)

    return EventSourceResponse(event_generator())


@app.get("/dashboard/{widget_id}/page", summary="Live dashboard HTML page (authenticated via query param)")
def dashboard_page(widget_id: int):
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Dashboard — Widget {widget_id}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f0f2f5; color: #1a202c; min-height: 100vh; }}
  .header {{ background: linear-gradient(135deg, #2b6cb0, #2c5282); color: #fff; padding: 24px 20px; }}
  .header h1 {{ font-size: 1.3rem; font-weight: 600; }}
  .header .sub {{ font-size: 0.8rem; opacity: 0.8; margin-top: 4px; }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}
  .auth-bar {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px 20px;
               display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 20px;
               box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .auth-bar input {{ flex: 1; min-width: 200px; padding: 8px 12px; border: 1px solid #e2e8f0;
                     border-radius: 6px; font-size: 13px; outline: none; }}
  .auth-bar input:focus {{ border-color: #4299e1; box-shadow: 0 0 0 3px rgba(66,153,225,0.12); }}
  .auth-bar button {{ padding: 8px 18px; border: none; border-radius: 6px; font-size: 13px;
                      font-weight: 600; cursor: pointer; transition: background 0.15s; }}
  .btn-primary {{ background: #4299e1; color: #fff; }}
  .btn-primary:hover {{ background: #3182ce; }}
  .btn-danger {{ background: #e53e3e; color: #fff; }}
  .btn-danger:hover {{ background: #c53030; }}
  .status {{ font-size: 0.78rem; padding: 0 4px; margin-bottom: 16px; display: flex; align-items: center; gap: 6px; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dot.green {{ background: #38a169; box-shadow: 0 0 6px rgba(56,161,105,0.4); }}
  .dot.red {{ background: #e53e3e; }}
  .dot.gray {{ background: #a0aec0; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 16px;
           text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .stat .num {{ font-size: 1.8rem; font-weight: 700; color: #2b6cb0; line-height: 1; }}
  .stat .label {{ font-size: 0.75rem; color: #718096; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .table-wrap {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden;
                 box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .table-header {{ padding: 14px 16px; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between;
                   align-items: center; }}
  .table-header h2 {{ font-size: 0.95rem; font-weight: 600; color: #2d3748; }}
  .row-count {{ font-size: 0.75rem; color: #a0aec0; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 10px 16px; font-size: 0.75rem; font-weight: 600; color: #718096;
       text-transform: uppercase; letter-spacing: 0.5px; background: #f7fafc; border-bottom: 1px solid #e2e8f0; }}
  td {{ padding: 10px 16px; font-size: 0.85rem; border-bottom: 1px solid #f0f0f0; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.new {{ animation: flash 0.8s ease; }}
  @keyframes flash {{ 0%{{ background:#ebf8ff; }} 100%{{ background:#fff; }} }}
  .empty {{ text-align: center; padding: 48px 20px; color: #a0aec0; font-size: 0.9rem; }}
  .empty .icon {{ font-size: 2rem; margin-bottom: 8px; }}
  .consent-yes {{ color: #38a169; font-weight: 500; }}
  .consent-no {{ color: #e53e3e; font-weight: 500; }}
  @media (max-width: 600px) {{
    .auth-bar {{ flex-direction: column; }}
    .auth-bar input {{ min-width: auto; width: 100%; }}
    table {{ font-size: 0.8rem; }}
    th, td {{ padding: 8px 10px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <h1>Live Dashboard</h1>
  <div class="sub">Widget #{widget_id} &mdash; Real-time submissions via SSE</div>
</div>
<div class="wrap">
  <div class="auth-bar">
    <input id="token" type="password" placeholder="Paste your Supabase auth token...">
    <button class="btn-primary" onclick="connect()" id="connectBtn">Connect</button>
    <button class="btn-danger" onclick="disconnect()" style="display:none" id="disconnectBtn">Disconnect</button>
  </div>
  <div class="status" id="status"><span class="dot gray"></span> Not connected</div>
  <div class="stats">
    <div class="stat"><div class="num" id="total">-</div><div class="label">Total</div></div>
    <div class="stat"><div class="num" id="today">-</div><div class="label">Today</div></div>
    <div class="stat"><div class="num" id="confirmed">-</div><div class="label">Confirmed</div></div>
  </div>
  <div class="table-wrap">
    <div class="table-header">
      <h2>Recent Submissions</h2>
      <span class="row-count" id="rowCount">0 rows</span>
    </div>
    <table>
      <thead><tr><th>ID</th><th>Email</th><th>Country</th><th>Consent</th><th>Time</th></tr></thead>
      <tbody id="feed"><tr><td colspan="5" class="empty"><div class="icon">&#128202;</div>Connect to see live submissions</td></tr></tbody>
    </table>
  </div>
</div>
<script>
var evtSource = null;
var MAX_ROWS = 100;
var savedToken = localStorage.getItem('dashboard_token_{widget_id}');
if (savedToken) {{
  document.getElementById('token').value = savedToken;
}}

function setStatus(html) {{
  document.getElementById('status').innerHTML = html;
}}

function connect() {{
  var token = document.getElementById('token').value.trim();
  if (!token) {{ alert('Paste your auth token'); return; }}
  localStorage.setItem('dashboard_token_{widget_id}', token);
  if (evtSource) evtSource.close();
  setStatus('<span class="dot gray"></span> Connecting...');
  document.getElementById('connectBtn').textContent = 'Connecting...';
  document.getElementById('connectBtn').disabled = true;

  evtSource = new EventSource('/dashboard/{widget_id}/events?token=' + encodeURIComponent(token));
  evtSource.onopen = function() {{
    setStatus('<span class="dot green"></span> Connected &mdash; listening for new submissions');
    document.getElementById('connectBtn').style.display = 'none';
    document.getElementById('disconnectBtn').style.display = '';
    document.getElementById('connectBtn').disabled = false;
    document.getElementById('connectBtn').textContent = 'Connect';
    loadStats(token);
  }};
  evtSource.addEventListener('new_submission', function(e) {{
    var sub = JSON.parse(e.data);
    addRow(sub);
    loadStats(token);
  }});
  evtSource.onerror = function() {{
    setStatus('<span class="dot red"></span> Disconnected &mdash; click Connect to retry');
    document.getElementById('connectBtn').style.display = '';
    document.getElementById('disconnectBtn').style.display = 'none';
    document.getElementById('connectBtn').textContent = 'Connect';
    document.getElementById('connectBtn').disabled = false;
  }};
}}

function disconnect() {{
  if (evtSource) {{ evtSource.close(); evtSource = null; }}
  setStatus('<span class="dot gray"></span> Disconnected');
  document.getElementById('connectBtn').style.display = '';
  document.getElementById('disconnectBtn').style.display = 'none';
}}

function escapeHtml(str) {{
  var div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}}

function addRow(sub) {{
  var tbody = document.getElementById('feed');
  if (tbody.querySelector('.empty')) tbody.innerHTML = '';
  var tr = document.createElement('tr');
  tr.className = 'new';
  var td1 = document.createElement('td');
  td1.textContent = sub.id;
  var td2 = document.createElement('td');
  td2.textContent = (sub.data && sub.data.email) ? sub.data.email : '-';
  var td3 = document.createElement('td');
  td3.textContent = sub.country || '-';
  var td4 = document.createElement('td');
  td4.className = sub.consent_given ? 'consent-yes' : 'consent-no';
  td4.textContent = sub.consent_given ? 'Yes' : 'No';
  var td5 = document.createElement('td');
  td5.textContent = new Date(sub.created_at).toLocaleTimeString();
  tr.appendChild(td1);
  tr.appendChild(td2);
  tr.appendChild(td3);
  tr.appendChild(td4);
  tr.appendChild(td5);
  tbody.insertBefore(tr, tbody.firstChild);
  var rows = tbody.querySelectorAll('tr').length;
  document.getElementById('rowCount').textContent = rows + ' row' + (rows !== 1 ? 's' : '');
  while (tbody.children.length > MAX_ROWS) {{
    tbody.removeChild(tbody.lastChild);
  }}
}}

function loadStats(token) {{
  fetch('/dashboard/{widget_id}/stats', {{headers:{{'Authorization':'Bearer '+token}}}})
    .then(function(r) {{ return r.json(); }})
    .then(function(s) {{
      document.getElementById('total').textContent = s.total_submissions;
      var today = new Date().toISOString().slice(0,10);
      var todayEntry = s.by_day.find(function(d){{ return d.day === today; }});
      document.getElementById('today').textContent = todayEntry ? todayEntry.count : 0;
      document.getElementById('confirmed').textContent = s.confirmed_count || '-';
    }}).catch(function() {{
      document.getElementById('total').textContent = '!';
    }});
}}
</script>
</body>
</html>""",
        status_code=200,
    )


# --- Public submission endpoint ---

@app.post("/submissions", status_code=201, summary="Public, cross-origin submission endpoint")
def create_submission(payload: SubmissionPayload, request: Request, background_tasks: BackgroundTasks):
    global BOT_REJECTED, BOT_ACCEPTED
    client_ip = request.client.host if request.client else "unknown"

    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    if is_rate_limited(f"widget:{payload.widget_id}"):
        raise HTTPException(status_code=429, detail="Too many requests for this widget. Please slow down.")

    widget = get_widget_by_id(payload.widget_id)
    if widget is None:
        raise HTTPException(status_code=400, detail=f"Widget {payload.widget_id} does not exist")

    if payload.website.strip():
        return JSONResponse(status_code=200, content={"status": "ok", "spam_flag": True})

    validate_data_against_fields(payload.data, widget["fields"])

    if not bot.verify(payload.widget_id, client_ip, payload.proof, POW_DIFFICULTY_BITS):
        BOT_REJECTED += 1
        raise HTTPException(status_code=400, detail="Bot check failed: missing or invalid proof-of-work")
    BOT_ACCEPTED += 1

    geo = enrich_ip(client_ip)

    confirmation_token = secrets.token_urlsafe(32)
    consent_ts = datetime.datetime.now(datetime.timezone.utc) if payload.consent_given else None

    row, deduplicated = insert_submission(
        widget_id=payload.widget_id,
        tenant_id=widget["tenant_id"],
        data=payload.data,
        ip=client_ip,
        country=geo["country"],
        city=geo["city"],
        spam_flag=False,
        idempotency_key=payload.idempotency_key,
        confirmation_token=confirmation_token,
        consent_given=payload.consent_given,
        consent_timestamp=consent_ts,
    )

    if not deduplicated:
        background_tasks.add_task(send_confirmation_email_safe, payload.data, confirmation_token)

    bus.publish(payload.widget_id, {
        "id": row["id"],
        "data": payload.data,
        "country": geo["country"],
        "city": geo["city"],
        "consent_given": payload.consent_given,
        "created_at": row["created_at"].isoformat(),
    })

    return {
        "id": row["id"],
        "created_at": row["created_at"].isoformat(),
        "spam_flag": False,
        "geo": geo,
        "deduplicated": deduplicated,
    }


# --- Double opt-in confirmation ---

@app.get("/confirm-email", summary="Confirm email via token (public, double opt-in)")
def confirm_email(token: str):
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")
    submission = confirm_submission(token)
    if submission is None:
        return HTMLResponse(
            content="<h2>Invalid or expired confirmation link.</h2><p>This email may have already been confirmed.</p>",
            status_code=400,
        )
    base = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
    return HTMLResponse(
        content=f"""<h2>Email Confirmed</h2>
<p>Thank you! Your submission (ID: {submission['id']}) has been confirmed.</p>
<p><a href="{base}">Back to home</a></p>""",
        status_code=200,
    )


# --- GDPR endpoints (authenticated, tenant-scoped) ---

class GdprExportRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or "@" not in value:
            raise ValueError("valid email required")
        return value


class GdprDeleteRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or "@" not in value:
            raise ValueError("valid email required")
        return value


@app.get("/gdpr/export/{widget_id}", summary="Export all submissions for a widget (authenticated, tenant-scoped)")
def gdpr_export_submissions(widget_id: int, tenant_id: int = Depends(get_current_tenant_id)):
    widget = get_widget_for_tenant(widget_id, tenant_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    rows = get_submissions_for_export(widget_id, tenant_id)
    return {
        "widget_id": widget_id,
        "total": len(rows),
        "submissions": [
            {
                "id": r["id"],
                "data": r["data"],
                "ip_address": r["ip_address"],
                "country": r["country"],
                "city": r["city"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "confirmed_at": r["confirmed_at"].isoformat() if r["confirmed_at"] else None,
                "consent_given": r["consent_given"],
                "consent_timestamp": r["consent_timestamp"].isoformat() if r["consent_timestamp"] else None,
            }
            for r in rows
        ],
    }


@app.post("/gdpr/export-by-email/{widget_id}", summary="Export submissions by email address (authenticated, tenant-scoped)")
def gdpr_export_by_email(widget_id: int, payload: GdprExportRequest,
                         tenant_id: int = Depends(get_current_tenant_id)):
    widget = get_widget_for_tenant(widget_id, tenant_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    conn = get_db()
    rows = conn.execute(
        """SELECT id, data, ip_address, country, city, created_at, confirmed_at,
                  consent_given, consent_timestamp
           FROM submissions
           WHERE widget_id = %s AND tenant_id = %s AND deleted_at IS NULL
             AND data->>'email' = %s
           ORDER BY created_at""",
        (widget_id, tenant_id, payload.email),
    ).fetchall()
    conn.close()
    return {
        "widget_id": widget_id,
        "email": payload.email,
        "total": len(rows),
        "submissions": [
            {
                "id": r["id"],
                "data": r["data"],
                "ip_address": r["ip_address"],
                "country": r["country"],
                "city": r["city"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "confirmed_at": r["confirmed_at"].isoformat() if r["confirmed_at"] else None,
                "consent_given": r["consent_given"],
                "consent_timestamp": r["consent_timestamp"].isoformat() if r["consent_timestamp"] else None,
            }
            for r in rows
        ],
    }


@app.delete("/gdpr/delete-by-email/{widget_id}", summary="Delete submissions by email (GDPR right to erasure, authenticated, tenant-scoped)")
def gdpr_delete_by_email(widget_id: int, payload: GdprDeleteRequest,
                         tenant_id: int = Depends(get_current_tenant_id)):
    widget = get_widget_for_tenant(widget_id, tenant_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    count = soft_delete_by_email(payload.email, widget_id, tenant_id)
    return {
        "widget_id": widget_id,
        "email": payload.email,
        "deleted_count": count,
    }