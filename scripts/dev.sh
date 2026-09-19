#!/usr/bin/env sh
# Boot the whole platform: Postgres via Docker, then the FastAPI app.
# The customer-site test page runs separately: `python -m http.server 5500 --directory customer-site`
set -e
docker compose up -d db
python -m uvicorn main:app --host 127.0.0.1 --port 8003