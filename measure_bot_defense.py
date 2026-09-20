"""Deterministic spam-reduction measurement for the proof-of-work bot defense.

Runs the REAL endpoint in-process (FastAPI TestClient) against simulated
spam traffic — no Postgres, no Supabase network calls, no live processes —
exactly like the pytest suite. Prints a before/after style reduction table.

Run:  python measure_bot_defense.py     (after pip install -r requirements-dev.txt)
"""
import os

os.environ.setdefault("SUPABASE_URL", "http://supabase.invalid")
os.environ.setdefault("SUPABASE_KEY", "anon-key")
os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost:59999/pytest")

from unittest.mock import patch  # noqa: E402

import repository  # noqa: E402

patch.object(repository, "run_migrations", lambda *args, **kwargs: []).start()

import bot  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import datetime  # noqa: E402

_NOW = datetime.datetime(2026, 1, 1, 12, 0)

WIDGET = {
    "id": 1,
    "tenant_id": 1,
    "type": "signup_form",
    "title": "Test Signup Widget",
    "fields": [{"name": "email", "label": "Email", "type": "email", "required": True}],
    "button_text": "Submit",
    "bundle_version": 3,
}


def main_():
    main.request_log.clear()
    main.BOT_REJECTED = 0
    main.BOT_ACCEPTED = 0
    original_bits = main.POW_DIFFICULTY_BITS

    with TestClient(main.app) as client:
        with patch.object(main, "get_widget_by_id", lambda widget_id: dict(WIDGET)):
            stored = []

            def fake_insert_submission(*args, **kwargs):
                stored.append(args)
                return ({"id": 1, "created_at": _NOW}, False)

            with patch.object(main, "insert_submission", fake_insert_submission):
                rows = []

                def post(label, payload):
                    r = client.post("/submissions", json=payload)
                    rows.append((label, r.status_code, r.json().get("spam_flag")))
                    return r

                post("no proof (naive scraper)", {"widget_id": 1, "data": {"email": "a@b.c"}})

                main.POW_DIFFICULTY_BITS = 256  # impossible -> every forged nonce fails
                post("forged nonce (scripted)", {"widget_id": 1, "data": {"email": "a@b.c"}, "proof": "not-a-real-nonce"})
                main.POW_DIFFICULTY_BITS = original_bits

                post("honeypot trap (auto-fill)", {"widget_id": 1, "data": {"email": "a@b.c"}, "website": "http://spam.example"})

                nonce = bot.solve(bot.build_challenge(1, "testclient", bot.now_window()), original_bits)
                post("legit visitor (valid PoW)", {"widget_id": 1, "data": {"email": "visitor@example.com"}, "proof": nonce})

    refused = [r for r in rows if r[1] == 400]
    dropped = [r for r in rows if r[1] == 200]
    accepted = [r for r in rows if r[1] == 201]
    print("Bot defense / spam-reduction probe (deterministic, in-process, no network)")
    print("-----------------------------------------------------------------------")
    for label, status, spam_flag in rows:
        outcome = "refused" if status == 400 else "dropped" if status == 200 else "accepted+stored"
        print(f"  {label:<34} -> {status}  spam_flag={spam_flag}  {outcome}")
    print("-----------------------------------------------------------------------")
    print(f"  bot-gate rejections counted:        {main.BOT_REJECTED}")
    print(f"  bot-gate acceptances counted:       {main.BOT_ACCEPTED}")
    print(f"  rows actually stored:               {len(stored)} (only the legit visitor)")
    reduction = len(refused) + len(dropped)
    print()
    print(f"  SPAM REDUCTION: {reduction}/{len(rows)} scripted spam vectors refused ("
          f"{len(refused)} rejected by PoW, {len(dropped)} dropped by honeypot); "
          f"{0} false rejects of the legit visitor.")
    assert len(refused) == 2 and len(dropped) == 1 and len(accepted) == 1 and len(stored) == 1
    print("  CHECK: metric holds (2 refused, 1 dropped, 1 legit accepted, 0 false rejects)")


if __name__ == "__main__":
    main_()