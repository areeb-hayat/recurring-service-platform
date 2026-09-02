"""TenantContext — the mandatory scoping argument.

P0 §3.4 (defence 2) and SEC-3: every repository/service entry point that touches
tenant-owned data takes an explicit :class:`TenantContext` derived from the
authenticated principal. There is deliberately no ``get_customer(id)`` overload
that could omit the tenant, and the tenant is never read from a request body.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from app.core.clock import Clock, business_date
from app.core.errors import PermissionDeniedError

__all__ = ["Principal", "TenantContext", "PlatformContext", "Scope"]

Scope = Literal["TENANT", "PLATFORM"]


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller. ``tenant_id`` is None exactly for platform scope."""

    user_id: uuid.UUID
    role: str
    scope: Scope
    tenant_id: uuid.UUID | None

    @property
    def is_platform(self) -> bool:
        return self.scope == "PLATFORM"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Everything a tenant-scoped write needs, resolved server-side.

    ``timezone`` and ``today`` come from the tenant row, so "today" is never the
    caller's opinion (P0 R4). ``now`` is the same injected instant that produced
    ``today``, so a business date and the timestamp written beside it can never
    disagree.

    ``cycle_type`` and ``cycle_start_day`` are configuration too (P0 §13): the
    billing period is read from the tenant row, never hard-coded.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    timezone: str
    now: datetime
    today: date
    unit_label: str
    currency: str
    currency_exponent: int
    cycle_type: str
    cycle_start_day: int

    @classmethod
    def build(cls, *, principal: Principal, tenant, clock: Clock) -> "TenantContext":
        if principal.is_platform or principal.tenant_id is None:
            # SEC-6: a platform principal has no tenant business authority.
            raise PermissionDeniedError(
                "platform principals cannot access tenant business data"
            )
        if principal.tenant_id != tenant.id:
            raise PermissionDeniedError("principal does not belong to this tenant")
        now = clock.now_utc()
        return cls(
            tenant_id=tenant.id,
            user_id=principal.user_id,
            role=principal.role,
            timezone=tenant.timezone,
            now=now,
            today=business_date(now, tenant.timezone),
            unit_label=tenant.unit_label,
            currency=tenant.currency,
            currency_exponent=tenant.currency_exponent,
            cycle_type=tenant.cycle_type,
            cycle_start_day=tenant.cycle_start_day,
        )


@dataclass(frozen=True, slots=True)
class PlatformContext:
    """Platform-scope authority over one explicitly chosen tenant (P0 §11).

    The mirror image of :class:`TenantContext`, and deliberately a separate type
    rather than a flag on it. A tenant principal can never produce one, and the
    target ``tenant_id`` comes from the platform caller's explicit choice rather
    than from a token claim — which is the whole difference between "the owner
    reading their own data" and "the platform acting on a named tenant".

    It exposes ``tenant_id`` and ``user_id`` with the same names
    :func:`~app.sync.idempotency.execute_idempotent` reads, so platform commands
    get the existing idempotency register rather than a second one.

    It carries no ``unit_label``, ``cycle_type`` or ``cycle_start_day``: commission
    is not a billing operation and has no business touching the tenant's cycle
    configuration. ``currency`` is present only to check that a plan is written in
    the tenant's own currency.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    timezone: str
    now: datetime
    today: date
    currency: str
    currency_exponent: int

    @classmethod
    def build(cls, *, principal: Principal, tenant, clock: Clock) -> "PlatformContext":
        if not principal.is_platform or principal.tenant_id is not None:
            # SEC-6 / COM-7, from the other side: a tenant principal has no
            # platform authority over anyone, including itself.
            raise PermissionDeniedError(
                "only a platform principal may act in platform scope"
            )
        now = clock.now_utc()
        return cls(
            tenant_id=tenant.id,
            user_id=principal.user_id,
            role=principal.role,
            timezone=tenant.timezone,
            now=now,
            today=business_date(now, tenant.timezone),
            currency=tenant.currency,
            currency_exponent=tenant.currency_exponent,
        )
