"""Pytest bootstrap for the deterministic API test suite.
"""
import os

os.environ.setdefault("SUPABASE_URL", "http://supabase.invalid")
os.environ.setdefault("SUPABASE_KEY", "anon-key")
os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost:59999/pytest")

from unittest.mock import patch  # noqa: E402

import repository  # noqa: E402

patch.object(repository, "run_migrations", lambda *args, **kwargs: []).start()

import main  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.request_log.clear()
    yield
    main.request_log.clear()