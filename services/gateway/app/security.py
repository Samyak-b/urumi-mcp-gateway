"""Password hashing and hashed tokens (API keys, invite links)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# ---- Passwords: scrypt (memory-hard, in the standard library) with a random salt per user ----
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=_N, r=_R, p=_P, dklen=32)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


# ---- API keys and invite tokens ----
# 256 random bits, so a fast hash is enough: unlike a password, a key can't be guessed from its hash. A fast
# hash also lets every /mcp request find its key with one indexed lookup.
KEY_PREFIX = "mcpgw_"


def new_api_key() -> tuple[str, str, str]:
    """Return (key, display prefix, sha256 hex). Only the last two are stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:12], hash_token(key)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
