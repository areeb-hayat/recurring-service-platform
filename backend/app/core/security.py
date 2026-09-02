"""Password hashing, refresh-token hashing and access-token encoding.

SEC-11: passwords stored only as a modern slow hash (Argon2id); refresh tokens
stored only as a SHA-256 hash of an opaque random value, and revocable.

P0 §3.3: short-lived JWT access token (60 min) + opaque DB-stored refresh token
(30 days). Deliberately small — no auth framework.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import AuthenticationError

__all__ = [
    "hash_password",
    "verify_password",
    "generate_refresh_token",
    "hash_refresh_token",
    "encode_access_token",
    "decode_access_token",
    "JWT_ALGORITHM",
]

JWT_ALGORITHM = "HS256"

_password_hasher = PasswordHasher()



def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time-ish verification. Never raises for a wrong password."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_refresh_token() -> str:
    """Opaque, high-entropy. The plaintext is returned to the client exactly once."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """SHA-256 is correct here: the input is already 384 bits of entropy, so a
    slow KDF buys nothing and would cost a hash on every refresh."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def encode_access_token(
    *,
    secret: str,
    user_id: str,
    scope: str,
    role: str,
    tenant_id: str | None,
    issued_at: datetime,
    expires_in_minutes: int,
) -> str:
    payload: dict[str, Any] = {
        "sub": user_id,
        "scope": scope,
        "role": role,
        "tenant_id": tenant_id,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(minutes=expires_in_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(*, secret: str, token: str, now: datetime | None = None) -> dict[str, Any]:
    """Decode and validate. Raises :class:`AuthenticationError` on any problem.

    Signature and claim presence are verified by PyJWT; **expiry is checked here**
    against ``now``, which the API layer supplies from the injected clock. That
    keeps one source of time in the application: tests can drive expiry exactly
    rather than sleeping, and the clock cannot disagree with itself.
    """
    options = {"require": ["exp", "iat", "sub"], "verify_exp": False}
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options=options)
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid access token") from exc

    reference = now or utcnow()
    if int(payload["exp"]) <= int(reference.timestamp()):
        raise AuthenticationError("access token has expired", code="TOKEN_EXPIRED")
    return payload


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
