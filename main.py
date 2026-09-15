import time
from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from repository import init_db, get_widget_by_id, insert_submission
from geo import enrich_ip

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
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
    website: str = ""  # honeypot field

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


@app.get("/health")
def health():
    return {"status": "ok"}


def validate_data_against_fields(data: dict, fields) -> None:
    allowed = {f["name"] for f in fields}
    required = {f["name"] for f in fields if f.get("required")}
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown fields: {sorted(unknown)}")
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {sorted(missing)}")


@app.post("/submissions", status_code=201, summary="Public, cross-origin submission endpoint")
def create_submission(payload: SubmissionPayload, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    widget = get_widget_by_id(payload.widget_id)
    if widget is None:
        raise HTTPException(status_code=400, detail=f"Widget {payload.widget_id} does not exist")

    if payload.website.strip():
        # Honeypot filled = bot. Return success but store nothing.
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