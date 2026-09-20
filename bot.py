"""Stateless proof-of-work bot defense.

The embed fetches a challenge for its (widget_id, client IP, 5-minute window)
and finds a nonce such that SHA-256(challenge:nonce) begins with `difficulty`
zero bits. The server recomputes the *same* challenge string from the
request's own inputs at verification time, so nothing is stored server-side:
no session, no table, no expiry job.

difficulty 0 (env POW_DIFFICULTY_BITS=0) disables the gate entirely — every
submission passes — which is also how bots are supposed to be kept out: the
cost is in the client's solve loop, not in the verify hash.
"""

import hashlib
import time

WINDOW_SECONDS = 300
"""Challenge rotates every 5 minutes. Replay within a window is still bounded
by the per-IP / per-widget rate limiter (5 requests / 10 s)."""


def now_window(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // WINDOW_SECONDS)


def build_challenge(widget_id: int, ip: str, window: int) -> str:
    return f"widget:{widget_id}:ip:{ip}:window:{window}"


def _leading_zero_bits(digest: bytes) -> int:
    """Count leading zero bits of a digest, byte by byte."""
    leading = 0
    for byte in digest:
        if byte == 0:
            leading += 8
        else:
            leading += 8 - byte.bit_length()
            break
    return leading


def _has_difficulty(digest: bytes, bits: int) -> bool:
    if bits <= 0:
        return True
    return _leading_zero_bits(digest) >= bits


def solve(challenge: str, bits: int, max_attempts: int = 2 ** 21) -> str:
    """Try nonces 0, 1, 2, ... until one satisfies the difficulty. Returns its
    string form, or "" if nothing was found within max_attempts."""
    if bits <= 0:
        return ""
    for nonce in range(max_attempts):
        digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()
        if _has_difficulty(digest, bits):
            return str(nonce)
    return ""


def verify(widget_id: int, ip: str, nonce: str, bits: int) -> bool:
    """Recompute the current challenge for this widget+IP and check the nonce."""
    if bits <= 0:
        return True
    if not isinstance(nonce, str) or nonce == "":
        return False
    challenge = build_challenge(widget_id, ip, now_window())
    digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()
    return _has_difficulty(digest, bits)