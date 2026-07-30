"""Small shared helpers: monotonic wall-clock in ms, id generation, hashing."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time


def now_ms() -> int:
    """Unix time in milliseconds. The one time unit the whole system speaks."""
    return int(time.time() * 1000)


def gen_cmd_id() -> str:
    """A short, unique correlation id echoed by the node in results and output."""
    return "c_" + secrets.token_hex(4)


def gen_token() -> str:
    """A fresh shared node token. Shown once, stored only as a hash."""
    return secrets.token_urlsafe(24)


def gen_nonce() -> str:
    """A ping nonce for RTT correlation."""
    return secrets.token_hex(4)


def hash_token(token: str) -> str:
    """Hash a token for storage. The plaintext is never persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, stored_hash: str) -> bool:
    """Constant-time compare of a presented token against the stored hash."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)
