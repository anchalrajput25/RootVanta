"""Password hashing and access tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

import jwt

from incident_agent.domain.models import utcnow

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def create_access_token(user_id: str, org_id: str, secret: str, ttl_minutes: int) -> str:
    now = utcnow()
    claims = {"sub": user_id, "org": org_id, "iat": now, "exp": now + timedelta(minutes=ttl_minutes)}
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "org", "exp"]})
