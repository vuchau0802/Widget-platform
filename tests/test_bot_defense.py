"""Deterministic proof-of-work bot-defense tests + the spam-reduction metric.

Covers the brief's bot-defense item:
  * the embed-facing /challenge endpoint (no-store, difficulty, bounded window);
  * every non-proof spam vector is refused BEFORE storage (insert never called);
  * a genuinely solved proof flows through the real verify logic to a 201;
  * the underlying bit-difficulty counter is tested on crafted digests so the
    "reject" paths are deterministic (no flaky 1/32 hash luck).

The reduction metric is captured in test_spam_reduction_metric: three scripted
spam vectors, every one refused, zero bot rows stored, the one legitimate
visitor accepted and stored exactly once.
"""
import datetime

import pytest

import bot
import main


@pytest.mark.parametrize("vector_name,proof", [
    ("naive scraper (no proof)", ""),
    ("forged/garbage nonce", "not-a-real-nonce"),
])
def test_spam_vectors_are_refused_and_not_stored(client, known_widget, monkeypatch, vector_name, proof):
    stored = []
    monkeypatch.setattr(main, "insert_submission", lambda *a, **k: stored.append(a))
    if proof:
        # 256 zero bits is computationally impossible -> every nonce fails
        monkeypatch.setattr(main, "POW_DIFFICULTY_BITS", 256)

    r = client.post("/submissions", json={"widget_id": 1, "data": {"email": "a@b.c"}, "proof": proof})
    assert r.status_code == 400
    assert "proof" in r.json()["detail"].lower()
    assert stored == []  # refused at the boundary — never reaches the DB


def test_spam_reduction_metric(client, known_widget, monkeypatch):
    stored = []

    def fake_insert(*args, **kwargs):
        stored.append(args)
        return ({"id": 77, "created_at": datetime.datetime(2026, 1, 1, 12, 0)}, False)

    monkeypatch.setattr(main, "insert_submission", fake_insert)
    original_bits = main.POW_DIFFICULTY_BITS

    no_proof = client.post("/submissions", json={"widget_id": 1, "data": {"email": "a@b.c"}}).status_code

    monkeypatch.setattr(main, "POW_DIFFICULTY_BITS", 256)
    forged = client.post(
        "/submissions",
        json={"widget_id": 1, "data": {"email": "a@b.c"}, "proof": "not-a-real-nonce"},
    ).status_code

    monkeypatch.setattr(main, "POW_DIFFICULTY_BITS", original_bits)
    honeypot = client.post(
        "/submissions",
        json={"widget_id": 1, "data": {"email": "a@b.c"}, "website": "http://spam.example"},
    ).status_code

    nonce = bot.solve(bot.build_challenge(1, "testclient", bot.now_window()), original_bits)
    legit = client.post(
        "/submissions",
        json={"widget_id": 1, "data": {"email": "visitor@example.com"}, "proof": nonce},
    ).status_code

    refused = (no_proof, forged)  # both non-PoW vectors 400'd at the boundary
    dropped = honeypot
    accepted = legit

    message = (
        f"spam reduction: {len(refused)}/2 non-PoW vectors refused ({', '.join(map(str, refused))}), "
        f"honeypot dropped ({dropped}), legit accepted ({accepted}); "
        f"rows stored: {len(stored)} (only the legit visitor) -> "
        f"0/{len(refused)+1} spam rows stored, {0} false rejects of legit"
    )
    assert no_proof == 400 and forged == 400, message
    assert dropped == 200, message
    assert accepted == 201, message
    assert len(stored) == 1, message


def test_honeypot_vector_dropped_without_storage(client, known_widget, monkeypatch):
    stored = []
    monkeypatch.setattr(main, "insert_submission", lambda *a, **k: stored.append(a))
    r = client.post(
        "/submissions",
        json={"widget_id": 1, "data": {"email": "a@b.c"}, "website": "http://spam.example"},
    )
    assert r.status_code == 200
    assert r.json()["spam_flag"] is True
    assert stored == []


def test_legitimate_visitor_with_valid_proof_accepted(client, known_widget, store_result, make_proof):
    r = client.post(
        "/submissions",
        json={"widget_id": 1, "data": {"email": "visitor@example.com"}, "proof": make_proof()},
    )
    assert r.status_code == 201
    assert r.json()["spam_flag"] is False


def test_challenge_endpoint_is_no_store_and_bounded(client):
    r = client.get("/challenge?widget_id=1")
    assert r.status_code == 200
    body = r.json()
    assert body["widget_id"] == 1
    assert body["difficulty"] == main.POW_DIFFICULTY_BITS
    assert body["window_seconds"] == bot.WINDOW_SECONDS
    assert body["challenge"].startswith("widget:1:ip:testclient:window:")
    assert r.headers["cache-control"] == "no-store"


def test_proof_round_trip_through_real_verify(make_proof):
    nonce = make_proof(widget_id=7, ip="203.0.113.9")
    assert nonce != ""
    assert bot.verify(7, "203.0.113.9", nonce, main.POW_DIFFICULTY_BITS)


def test_challenge_binds_to_widget_and_ip():
    window = bot.now_window()
    assert bot.build_challenge(1, "1.1.1.1", window) != bot.build_challenge(2, "1.1.1.1", window)
    assert bot.build_challenge(1, "1.1.1.1", window) != bot.build_challenge(1, "2.2.2.2", window)
    assert bot.build_challenge(1, "1.1.1.1", window) != bot.build_challenge(1, "1.1.1.1", window + 1)


def test_difficulty_counter_is_deterministic():
    # Crafted digests, not hash luck: exact leading-zero-bit semantics.
    assert bot._has_difficulty(bytes([0x00, 0x01]), 5) is True   # 15 leading zeros
    assert bot._has_difficulty(bytes([0x00]), 5) is True          # 8 leading zeros
    assert bot._has_difficulty(bytes([0x10]), 5) is False         # 3 leading zeros < 5
    assert bot._has_difficulty(bytes([0x80, 0x00]), 5) is False   # first bit set
    assert bot._has_difficulty(bytes([0x00, 0x00]), 16) is True   # 16 leading zeros
    assert bot._has_difficulty(b"", 0) is True                    # difficulty 0 = gate off


def test_metrics_counters_track_gate(client, known_widget, store_result, make_proof):
    blocked_before = main.BOT_REJECTED
    accepted_before = main.BOT_ACCEPTED
    client.post("/submissions", json={"widget_id": 1, "data": {"email": "a@b.c"}})
    assert main.BOT_REJECTED == blocked_before + 1
    client.post(
        "/submissions",
        json={"widget_id": 1, "data": {"email": "visitor@example.com"}, "proof": make_proof()},
    )
    assert main.BOT_ACCEPTED == accepted_before + 1