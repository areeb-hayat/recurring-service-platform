"""FastAPI dependencies: session, clock, principal, tenant context, capabilities.

The dependency chain is where SEC-3/SEC-4/SEC-6 become structural:
``require_tenant_context`` derives the tenant from the *authenticated principal*,
never from a path, query or body parameter, and rejects platform principals.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.db import session_scope
from app.core.errors import AuthenticationError, JobEndpointDisabledError, NotFoundError
from app.core.security import decode_access_token
from app.identity.capabilities import require
from app.identity.service import principal_from_claims
from app.ports.comms import CommunicationProvider
from app.tenancy.context import PlatformContext, Principal, TenantContext
from app.tenancy.models import Tenant

__all__ = [
    "get_db",
    "get_clock",
    "get_current_principal",
    "require_tenant_context",
    "build_platform_context",
    "require_capability",
    "get_communication_provider",
    "require_job_secret",
    "CurrentPrincipal",
    "Db",
]


def get_db(request: Request) -> Iterator[Session]:
    """One session per request. Overridden in tests to share the test session."""
    factory = getattr(request.app.state, "session_factory", None)
    session = factory() if factory is not None else session_scope()
    try:
        yield session
    finally:
        session.close()


def get_clock(request: Request) -> Clock:
    """Injected so tests can freeze time (P0 R4 midnight boundaries)."""
    return getattr(request.app.state, "clock", None) or SystemClock()


def get_app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


Db = Annotated[Session, Depends(get_db)]


def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_app_settings),
    clock: Clock = Depends(get_clock),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(
        secret=settings.require_jwt_secret(), token=token, now=clock.now_utc()
    )
    return principal_from_claims(claims)


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require_tenant_context(
    principal: CurrentPrincipal,
    session: Db,
    clock: Annotated[Clock, Depends(get_clock)],
) -> TenantContext:
    """Build the tenant scope for a business route.

    Raises 403 for a platform principal (SEC-6) and 404 for a tenant whose row
    is missing or suspended.
    """
    if principal.tenant_id is None:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(
            "platform principals cannot access tenant business data"
        )
    tenant = session.get(Tenant, principal.tenant_id)
    if tenant is None or tenant.status != "ACTIVE":
        raise NotFoundError("tenant not found")
    return TenantContext.build(principal=principal, tenant=tenant, clock=clock)


TenantCtx = Annotated[TenantContext, Depends(require_tenant_context)]


def build_platform_context(
    session: Session,
    clock: Clock,
    principal: Principal,
    tenant_id: uuid.UUID,
) -> PlatformContext:
    """Build the platform scope for a commission route (P0 §11, COM-7/COM-8).

    Not a plain ``Depends`` because the target tenant arrives in the query string
    on a read and in the body on a write; the check is identical either way and
    lives here rather than being repeated in each route.

    A tenant principal never reaches this — the capability dependency already
    refused it, since no tenant role holds any ``commission:*`` capability — and
    :meth:`PlatformContext.build` refuses it again if it somehow does.
    """
    tenant = session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != "ACTIVE":
        raise NotFoundError("tenant not found")
    return PlatformContext.build(principal=principal, tenant=tenant, clock=clock)


def require_capability(capability: str):
    """Route dependency factory: ``Depends(require_capability("customer:write"))``."""

    def _check(principal: CurrentPrincipal) -> Principal:
        require(principal, capability)
        return principal

    return _check


def get_communication_provider(request: Request) -> CommunicationProvider:
    """The configured delivery provider, built once per application.

    The **only** place the application chooses an implementation, which is what
    keeps A-SLOT-5 true: ``app/reminders`` imports the port and takes the
    provider as an argument, so no domain module has ever heard of an adapter.
    Tests override ``app.state.communication_provider`` with a fake and never
    make a live call.
    """
    provider = getattr(request.app.state, "communication_provider", None)
    if provider is None:
        from app.adapters.comms import build_communication_provider

        settings = get_app_settings(request)
        provider = build_communication_provider(settings.comms_provider)
        request.app.state.communication_provider = provider
    return provider


def require_job_secret(
    request: Request,
    x_job_secret: Annotated[str | None, Header()] = None,
) -> None:
    """Authenticate the host's cron against the internal job endpoint (P0 §12).

    A shared secret rather than a bearer token, because the caller is a crontab
    line and not a person: there is no user to attribute the run to, which is
    why everything it writes is audited as ``SYSTEM``/``JOB``.

    Three properties this must have, and each is deliberate:

    * **Never open.** An unset ``INTERNAL_JOB_SECRET`` disables the route (503),
      it does not skip the check. A public "send reminders to everybody" URL is
      the worst failure this endpoint could have.
    * **Constant-time.** Compared with :func:`hmac.compare_digest`, so the secret
      cannot be recovered a character at a time.
    * **No tenant authority.** Presenting it grants no scope over any tenant's
      data: the route takes no tenant parameter, and the runner builds its own
      per-tenant context from the tenants the *server* found.
    """
    settings = get_app_settings(request)
    try:
        expected = settings.require_internal_job_secret()
    except RuntimeError as exc:
        raise JobEndpointDisabledError(str(exc)) from exc
    if not x_job_secret or not hmac.compare_digest(x_job_secret, expected):
        raise AuthenticationError("invalid or missing job secret")
