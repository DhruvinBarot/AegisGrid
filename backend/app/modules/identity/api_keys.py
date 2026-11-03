"""API key format and secret hashing. Key: `agk_{live|test}_{key_id}_{secret}`.

`key_id` is 16 hex chars (stored in `api_keys.key_id`, used for lookup); the secret is URL-safe
and may contain `_`, so parse with a bounded split. Only HMAC-SHA256(pepper, secret) is stored.
"""

import hashlib
import hmac
import secrets
from typing import Literal

from app.core.config import settings

PREFIX = "agk"


def hash_secret(secret: str) -> bytes:
    pepper = settings.api_key_pepper.get_secret_value().encode()
    return hmac.new(pepper, secret.encode(), hashlib.sha256).digest()


def secret_matches(secret: str, stored: bytes) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored)


def generate(mode: Literal["live", "test"] = "live") -> tuple[str, str, bytes]:
    """Return (full key shown once, key_id, secret_hmac to store)."""
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return f"{PREFIX}_{mode}_{key_id}_{secret}", key_id, hash_secret(secret)


def parse(raw: str) -> tuple[str, str] | None:
    """Return (key_id, secret), or None if the format is wrong."""
    parts = raw.split("_", 3)
    if len(parts) != 4 or parts[0] != PREFIX or parts[1] not in ("live", "test"):
        return None
    _, _, key_id, secret = parts
    if len(key_id) != 16 or not secret:
        return None
    return key_id, secret
