"""The tenant's own configuration, as the UI needs to render it.

**Why this exists.** P0 §4 puts currency, currency exponent, unit label, timezone
and the entry defaults on the ``tenant`` row precisely so they are configuration
rather than code constants — but until P4 nothing exposed them. They reached a
caller only by serializing a *customer* or a *service record*, which means a
tenant with no customers and no records (the state every tenant is in
immediately after provisioning, which is exactly when its owner first opens
"Add customer") had no way to learn its own currency or unit label. A client that
guessed either one would be hard-coding business configuration, which is the
thing the tenant row exists to prevent.

``business_date`` is here for the same reason from the other direction: P0 R4
makes "today" the tenant's timezone's opinion, never the caller's. Without this
field a client can only find the business date by first calling a dated route
with a date it invented. It reads today from the injected clock and the tenant's
timezone — the same pair :class:`~app.tenancy.context.TenantContext` uses — so
the register can ask what day it is instead of deciding.

Read-only, and deliberately narrow: it returns the configuration a screen
renders and nothing else. Cycle configuration, the reminder schedule and the
tenant's status are not here because no P4 screen shows them, and an unused field
is a claim about a surface that has not been designed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.tenancy.context import TenantContext
from app.tenancy.models import Tenant

__all__ = ["tenant_settings"]


def tenant_settings(session: Session, ctx: TenantContext) -> dict[str, Any]:
    """SEC-3: the tenant comes from the authenticated context, never a parameter."""
    tenant = session.get(Tenant, ctx.tenant_id)
    if tenant is None:
        raise NotFoundError("tenant not found")
    return {
        "name": tenant.name,
        "currency": tenant.currency,
        "currency_exponent": tenant.currency_exponent,
        "unit_label": tenant.unit_label,
        "timezone": tenant.timezone,
        # P0 R4: the tenant's business date, resolved server-side.
        "business_date": ctx.today.isoformat(),
        # Entry defaults for a new customer (P0 §4). Quantity is a string for the
        # same reason it is everywhere else: it must never become a JSON float.
        "default_quantity": str(tenant.default_quantity),
        "default_unit_price_minor": tenant.default_unit_price_minor,
    }
