import os
import time
import json
from collections import defaultdict, deque
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse
from pydantic import BaseModel, field_validator
from repository import (
    init_db, get_widget_by_id, insert_submission, get_db,
    get_or_create_tenant, create_widget, list_widgets_for_tenant,
    get_widget_for_tenant, update_widget_for_tenant, delete_widget_for_tenant,
)
from geo import enrich_ip
from auth import supabase

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


def send_confirmation_email(data: dict) -> None:
    raise Exception("SMTP server unreachable (simulated failure for Phase 2 proof)")


def send_confirmation_email_safe(data: dict) -> None:
    """Runs in a FastAPI BackgroundTask (off the request path). Retries twice,
    then gives up — a failure must never break the already-stored submission."""
    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            send_confirmation_email(data)
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


class WidgetUpdate(BaseModel):
    title: str
    button_text: str = "Submit"

    @field_validator("title", "button_text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("cannot be empty")
        if len(value) > 200:
            raise ValueError("too long (max 200)")
        return value


@app.post("/widgets", status_code=201, summary="Create a widget (authenticated)")
def create_widget_route(payload: WidgetCreate, tenant_id: int = Depends(get_current_tenant_id)):
    widget = create_widget(tenant_id, payload.type, payload.title, payload.fields, payload.button_text)
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
    widget = update_widget_for_tenant(widget_id, tenant_id, payload.title, payload.button_text)
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
    }
    return Response(
        content=json.dumps(config),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/widget.js", summary="The versioned widget bundle, long-cached")
def get_widget_bundle(v: int = 1):
    return FileResponse(
        "static/widget.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
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

    geo = enrich_ip(client_ip)

    row, deduplicated = insert_submission(
        widget_id=payload.widget_id,
        tenant_id=widget["tenant_id"],
        data=payload.data,
        ip=client_ip,
        country=geo["country"],
        city=geo["city"],
        spam_flag=False,
        idempotency_key=payload.idempotency_key,
    )

    if not deduplicated:
        background_tasks.add_task(send_confirmation_email_safe, payload.data)

    return {
        "id": row["id"],
        "created_at": row["created_at"].isoformat(),
        "spam_flag": False,
        "geo": geo,
        "deduplicated": deduplicated,
    }