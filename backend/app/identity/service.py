"""Authentication: login, refresh, logout.

P0 §3.3 — short-lived JWT access token plus an opaque, DB-stored, revocable
refresh token. No public signup: tenant provisioning is a platform action
(P0 §4), so there is no registration function here at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import ActorScope, AuditAction, AuditSource
from app.audit.service import record_audit_event
from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.security import (
    encode_access_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.identity.models import AppUser, UserSession
from app.tenancy.context import Principal

__all__ = ["TokenPair", "login", "refresh", "logout", "principal_from_claims"]


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    role: str
    scope: str
    tenant_id: str | None


def _issue(
    session: Session, *, user: AppUser, settings: Settings, clock: Clock, device_label: str | None
) -> TokenPair:
    now = clock.now_utc()
    refresh_plaintext = generate_refresh_token()

    session.add(
        UserSession(
            tenant_id=user.tenant_id,
            user_id=user.id,
            # SEC-11: only the hash is stored; the plaintext leaves in the response.
            refresh_token_hash=hash_refresh_token(refresh_plaintext),
            issued_at=now,
            expires_at=now + timedelta(days=settings.refresh_token_days),
            device_label=device_label,
        )
    )

    access = encode_access_token(
        secret=settings.require_jwt_secret(),
        user_id=str(user.id),
        scope=user.scope,
        role=user.role,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        issued_at=now,
        expires_in_minutes=settings.access_token_minutes,
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh_plaintext,
        expires_in=settings.access_token_minutes * 60,
        role=user.role,
        scope=user.scope,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )


def login(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
    clock: Clock,
    device_label: str | None = None,
) -> TokenPair:
    """Authenticate by email + password.

    SEC-7: this only ever looks in ``app_user``. A customer has no credentials
    and therefore cannot be authenticated by any input.
    """
    user = session.execute(
        select(AppUser).where(func.lower(AppUser.email) == email.strip().lower())
    ).scalar_one_or_none()

    # Same error and roughly the same work for "no such user" and "wrong
    # password", so the response does not disclose which accounts exist.
    if user is None or not verify_password(user.password_hash, password):
        if user is not None:
            record_audit_event(
                session,
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
                actor_scope=ActorScope.TENANT if user.tenant_id else ActorScope.PLATFORM,
                action=AuditAction.AUTH_LOGIN_FAILED,
                entity_type="app_user",
                entity_id=user.id,
                source=AuditSource.ONLINE,
            )
            session.commit()
        raise AuthenticationError("invalid email or password")

    if user.status != "ACTIVE":
        raise AuthenticationError("account is disabled")

    tokens = _issue(
        session, user=user, settings=settings, clock=clock, device_label=device_label
    )
    record_audit_event(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        actor_scope=ActorScope.TENANT if user.tenant_id else ActorScope.PLATFORM,
        action=AuditAction.AUTH_LOGIN_SUCCEEDED,
        entity_type="app_user",
        entity_id=user.id,
        source=AuditSource.ONLINE,
    )
    session.commit()
    return tokens


def refresh(
    session: Session, *, refresh_token: str, settings: Settings, clock: Clock
) -> TokenPair:
    """Exchange a valid refresh token for a new pair, rotating the old one."""
    token_hash = hash_refresh_token(refresh_token)
    user_session = session.execute(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    ).scalar_one_or_none()

    now = clock.now_utc()
    if (
        user_session is None
        or user_session.revoked_at is not None
        or user_session.expires_at <= now
    ):
        raise AuthenticationError("invalid or expired refresh token")

    user = session.get(AppUser, user_session.user_id)
    if user is None or user.status != "ACTIVE":
        raise AuthenticationError("account is disabled")

    # Rotate: the presented token is single-use.
    user_session.revoked_at = now
    tokens = _issue(
        session,
        user=user,
        settings=settings,
        clock=clock,
        device_label=user_session.device_label,
    )
    record_audit_event(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        actor_scope=ActorScope.TENANT if user.tenant_id else ActorScope.PLATFORM,
        action=AuditAction.AUTH_TOKEN_REFRESHED,
        entity_type="app_user",
        entity_id=user.id,
        source=AuditSource.ONLINE,
    )
    session.commit()
    return tokens


def logout(session: Session, *, refresh_token: str, clock: Clock) -> None:
    """Revoke one refresh token. Idempotent and silent about unknown tokens."""
    token_hash = hash_refresh_token(refresh_token)
    user_session = session.execute(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    ).scalar_one_or_none()
    if user_session is None or user_session.revoked_at is not None:
        return
    user_session.revoked_at = clock.now_utc()
    record_audit_event(
        session,
        tenant_id=user_session.tenant_id,
        actor_user_id=user_session.user_id,
        actor_scope=ActorScope.TENANT if user_session.tenant_id else ActorScope.PLATFORM,
        action=AuditAction.AUTH_LOGGED_OUT,
        entity_type="app_user",
        entity_id=user_session.user_id,
        source=AuditSource.ONLINE,
    )
    session.commit()


def principal_from_claims(claims: dict) -> Principal:
    tenant_id = claims.get("tenant_id")
    return Principal(
        user_id=uuid.UUID(claims["sub"]),
        role=claims["role"],
        scope=claims["scope"],
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
    )
