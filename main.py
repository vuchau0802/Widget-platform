import time
import json
from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse
from pydantic import BaseModel, field_validator
from repository import init_db, get_widget_by_id, insert_submission, get_db
from geo import enrich_ip

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

init_db()

MAX_PAYLOAD_FIELDS = 20
MAX_FIELD_LENGTH = 2000

RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 10
request_log: dict[str, deque] = defaultdict(deque)


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    window = request_log[ip]
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

    @field_validator("data")
    @classmethod
    def validate_data_shape(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_PAYLOAD_FIELDS:
            raise ValueError(f"too many fields (max {MAX_PAYLOAD_FIELDS})")
        for key, val in value.items():
            if len(val) > MAX_FIELD_LENGTH:
                raise ValueError(f"field '{key}' exceeds max length ({MAX_FIELD_LENGTH})")
        return value


def send_confirmation_email(to_data: dict) -> None:
    raise Exception("SMTP server unreachable (simulated failure for Phase 2 proof)")


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


@app.get("/dashboard/{widget_id}/stats", summary="Aggregated submission stats for a widget")
def get_dashboard_stats(widget_id: int):
    # NOTE (honest gap, tracked in README limitations): the brief specifies this
    # should be an authenticated, tenant-scoped endpoint. Tenant auth (Supabase,
    # same pattern as BE-03) has not been wired into this endpoint yet.
    widget = get_widget_by_id(widget_id)
    if widget is None:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")

    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) AS count FROM submissions WHERE widget_id = %s", (widget_id,)
    ).fetchone()["count"]

    by_day = conn.execute(
        """SELECT DATE(created_at) AS day, COUNT(*) AS count
           FROM submissions WHERE widget_id = %s
           GROUP BY DATE(created_at) ORDER BY day DESC LIMIT 7""",
        (widget_id,),
    ).fetchall()

    by_geo = conn.execute(
        """SELECT COALESCE(country, 'Unknown') AS country, COUNT(*) AS count
           FROM submissions WHERE widget_id = %s
           GROUP BY country ORDER BY count DESC""",
        (widget_id,),
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
def create_submission(payload: SubmissionPayload, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    widget = get_widget_by_id(payload.widget_id)
    if widget is None:
        raise HTTPException(status_code=400, detail=f"Widget {payload.widget_id} does not exist")

    if payload.website.strip():
        return JSONResponse(status_code=200, content={"status": "ok", "spam_flag": True})

    validate_data_against_fields(payload.data, widget["fields"])

    geo = enrich_ip(client_ip)

    result = insert_submission(
        widget_id=payload.widget_id,
        tenant_id=widget["tenant_id"],
        data=payload.data,
        ip=client_ip,
        country=geo["country"],
        city=geo["city"],
        spam_flag=False,
    )

    try:
        send_confirmation_email(payload.data)
    except Exception as e:
        print(f"[side-effect-failure] confirmation email failed, submission still stored: {e}")

    return {
        "id": result["id"],
        "created_at": result["created_at"].isoformat(),
        "spam_flag": False,
        "geo": geo,
    }