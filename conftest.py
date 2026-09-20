"""Pytest bootstrap for the deterministic API test suite.
"""
import os

os.environ.setdefault("SUPABASE_URL", "http://supabase.invalid")
os.environ.setdefault("SUPABASE_KEY", "anon-key")
os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost:59999/pytest")

from unittest.mock import patch  # noqa: E402

import repository  # noqa: E402

patch.object(repository, "run_migrations", lambda *args, **kwargs: []).start()

import datetime  # noqa: E402

import bot  # noqa: E402
import main  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


WIDGET = {
    "id": 1,
    "tenant_id": 1,
    "type": "signup_form",
    "title": "Test Signup Widget",
    "fields": [{"name": "email", "label": "Email", "type": "email", "required": True}],
    "button_text": "Submit",
    "bundle_version": 3,
}


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def known_widget(monkeypatch):
    monkeypatch.setattr(main, "get_widget_by_id", lambda widget_id: dict(WIDGET))
    return WIDGET


@pytest.fixture()
def store_result(monkeypatch):
    """insert_submission returns (row, deduplicated); a test may swap .value."""

    class Box:
        value = ({"id": 77, "created_at": datetime.datetime(2026, 1, 1, 12, 0)}, False)

    monkeypatch.setattr(main, "insert_submission", lambda *a, **k: Box.value)
    return Box


@pytest.fixture()
def make_proof():
    """Solve the current proof-of-work challenge for a widget/IP in-process."""

    def _make(widget_id: int = 1, ip: str = "testclient", bits: int | None = None):
        bits = main.POW_DIFFICULTY_BITS if bits is None else bits
        challenge = bot.build_challenge(widget_id, ip, bot.now_window())
        return bot.solve(challenge, bits)

    return _make


@pytest.fixture(autouse=True)
def _reset_state():
    main.request_log.clear()
    main.BOT_REJECTED = 0
    main.BOT_ACCEPTED = 0
    yield
    main.request_log.clear()
    main.BOT_REJECTED = 0
    main.BOT_ACCEPTED = 0