"""Deterministic HTTP-behavior tests for POST /submissions and friends.

Covers the brief's list: invalid payloads (400/422/413), rate limiting
(per-IP + per-widget, sliding window), honeypot spam control, idempotency,
CORS on actual responses, and the local-IP geo row.

The SQL layer is stubbed via monkeypatch so results are independent of DB
seed state and no Postgres connection is used (migrations were stubbed in
conftest.py). Geo resolves deterministically because TestClient's IP is
"testclient" -> Testland/Localhost.
"""
import datetime
import json

import main

VALID_PAYLOAD = {"widget_id": 1, "data": {"email": "visitor@example.com"}}


# --- invalid payloads ---


def test_unknown_widget_on_submit_is_400(client, monkeypatch):
    monkeypatch.setattr(main, "get_widget_by_id", lambda widget_id: None)
    r = client.post("/submissions", json={"widget_id": 999, "data": {"email": "a@b.c"}})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_missing_required_field_is_400(client, known_widget):
    r = client.post("/submissions", json={"widget_id": 1, "data": {}})
    assert r.status_code == 400
    assert "Missing required fields" in r.json()["detail"]


def test_unknown_field_is_400(client, known_widget):
    r = client.post("/submissions", json={"widget_id": 1, "data": {"totally_unknown": "x"}})
    assert r.status_code == 400
    assert "Unknown fields" in r.json()["detail"]


def test_too_many_fields_is_422(client, known_widget):
    r = client.post("/submissions", json={"widget_id": 1, "data": {f"f{i}": "v" for i in range(21)}})
    assert r.status_code == 422


def test_overlong_field_value_is_422(client, known_widget):
    r = client.post("/submissions", json={"widget_id": 1, "data": {"email": "x" * 2001}})
    assert r.status_code == 422


def test_non_object_data_is_422(client, known_widget):
    r = client.post("/submissions", json={"widget_id": 1, "data": ["email"]})
    assert r.status_code == 422


def test_missing_widget_id_is_422(client, known_widget):
    r = client.post("/submissions", json={"data": {"email": "a@b.c"}})
    assert r.status_code == 422


def test_blank_idempotency_key_is_422(client, known_widget):
    r = client.post(
        "/submissions", json={"widget_id": 1, "data": {"email": "a@b.c"}, "idempotency_key": ""}
    )
    assert r.status_code == 422


def test_body_over_50kb_is_413(client):
    huge_body = json.dumps({"widget_id": 1, "data": {"padding": "x" * 60_000}})
    r = client.post(
        "/submissions",
        content=huge_body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


# --- happy path / CORS on the actual response / geo ---


def test_valid_submission_is_201_with_cors_and_geo(client, known_widget, store_result, make_proof):
    r = client.post(
        "/submissions",
        headers={"Origin": "http://example.com"},
        json={**VALID_PAYLOAD, "proof": make_proof()},
    )
    assert r.status_code == 201
    assert r.headers["access-control-allow-origin"] == "*"
    body = r.json()
    assert body["id"] == 77
    assert body["spam_flag"] is False
    assert body["deduplicated"] is False
    assert body["geo"] == {"country": "Testland", "city": "Localhost"}


# --- rate limiting ---


def test_rate_limiter_scoped_per_ip_and_widget(client):
    for _ in range(5):
        assert not main.is_rate_limited("widget:42")
    assert main.is_rate_limited("widget:42")
    assert not main.is_rate_limited("widget:99")
    assert not main.is_rate_limited("10.1.2.3")
    for _ in range(4):
        assert not main.is_rate_limited("10.1.2.3")
    assert main.is_rate_limited("10.1.2.3")


def test_rate_limiter_window_slides(client, monkeypatch):
    fake_now = [1_000.0]
    monkeypatch.setattr(main.time, "time", lambda: fake_now[0])
    for _ in range(5):
        assert not main.is_rate_limited("widget:7")
    assert main.is_rate_limited("widget:7")
    fake_now[0] += 11
    assert not main.is_rate_limited("widget:7")


def test_sixth_submission_in_window_is_429(client, known_widget, store_result, make_proof):
    payload = {**VALID_PAYLOAD, "proof": make_proof()}
    for _ in range(5):
        assert client.post("/submissions", json=payload).status_code == 201
    assert client.post("/submissions", json=payload).status_code == 429


# --- spam control + idempotency ---


def test_honeypot_filled_dropped_with_spam_flag(client, known_widget, monkeypatch):
    stored = []
    monkeypatch.setattr(main, "insert_submission", lambda *a, **k: stored.append(a))
    r = client.post(
        "/submissions",
        json={**VALID_PAYLOAD, "website": "http://spam.example"},
    )
    assert r.status_code == 200
    assert r.json()["spam_flag"] is True
    assert stored == []


def test_retried_idempotency_key_returns_deduplicated(client, known_widget, store_result, make_proof):
    payload = {**VALID_PAYLOAD, "idempotency_key": "request-9000", "proof": make_proof()}
    r1 = client.post("/submissions", json=payload)
    assert r1.status_code == 201
    assert r1.json()["deduplicated"] is False
    store_result.value = ({"id": 77, "created_at": datetime.datetime(2026, 1, 1, 12, 0)}, True)
    r2 = client.post("/submissions", json=payload)
    assert r2.status_code == 201
    assert r2.json()["deduplicated"] is True
    assert r2.json()["id"] == 77


# --- auth boundary ---


def test_widget_endpoint_requires_auth(client):
    assert client.get("/widgets").status_code == 401