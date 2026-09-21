import os
import time
import json
import secrets
import datetime
from collections import defaultdict, deque
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from pydantic import BaseModel, field_validator
from repository import (
    init_db, get_widget_by_id, insert_submission, get_db,
    get_or_create_tenant, create_widget, list_widgets_for_tenant,
    get_widget_for_tenant, update_widget_for_tenant, delete_widget_for_tenant,
    confirm_submission, get_submissions_for_export, soft_delete_by_email,
)
from geo import enrich_ip
from auth import supabase
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


def build_embed_snippet(widget_id: int) -> str:
    """The one line a customer pastes into their site. The public base URL is
    configurable via PUBLIC_BASE_URL so a real deployment points at its own API."""
    base = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
    return f'<script src="{base}/widget.js?id={widget_id}"></script>'


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


@app.get("/widget.js", summary="The embeddable widget bundle, long-cached")
def get_widget_bundle(v: int = 1):
    return FileResponse(
        "static/widget.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
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