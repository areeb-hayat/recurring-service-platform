"""The flat, static capability map (P0 §3.2).

Deliberately not an RBAC framework and not a permissions table: a dict and one
check function. If this grows past ~30 entries, revisit the model rather than
adding hierarchy.

The tenant and platform sets are **disjoint by construction** (SEC-5): that is
what makes "a business owner cannot reach platform commission authority" true
without any runtime cleverness.
"""

from __future__ import annotations

from app.core.errors import PermissionDeniedError
from app.identity.models import Role
from app.tenancy.context import Principal

__all__ = ["CAPABILITIES", "ALL_CAPABILITIES", "TENANT_CAPABILITIES", "has", "require"]

CAPABILITIES: dict[str, frozenset[str]] = {
    Role.OWNER_ADMIN: frozenset(
        {
            "customer:read",
            "customer:write",
            "service:record",
            "service:correct",
            "billing:read",
            "billing:close_cycle",
            "payment:record",
            "payment:void",
            "reminder:read",
            "reminder:trigger",
            "dashboard:read",
            "search:use",
            # P6: the owner's operating-cost record — what the business pays its
            # providers. Tenant business data, so it lives in the tenant set and
            # is deliberately NOT expressed through any ``commission:*``
            # capability: platform commission and company operating expenses are
            # two separate accounting concepts and must not share authority
            # (SEC-5 stays true — these are disjoint from the platform set).
            "cost:read",
            "cost:write",
        }
    ),
    Role.PLATFORM_OWNER: frozenset(
        {
            "commission:read",
            "commission:adjust",
            "commission:settle",
            "tenant:provision",
            "platform_dashboard:read",
        }
    ),
    # SEC-8: reserved for a future package. Grants nothing today.
    Role.OPERATOR: frozenset(),
}

ALL_CAPABILITIES: frozenset[str] = frozenset().union(*CAPABILITIES.values())
TENANT_CAPABILITIES: frozenset[str] = CAPABILITIES[Role.OWNER_ADMIN]
PLATFORM_CAPABILITIES: frozenset[str] = CAPABILITIES[Role.PLATFORM_OWNER]


def has(principal: Principal, capability: str) -> bool:
    return capability in CAPABILITIES.get(principal.role, frozenset())


def require(principal: Principal, capability: str) -> None:
    """Raise unless the principal's role holds ``capability``."""
    if capability not in ALL_CAPABILITIES:
        # A typo in a capability string must fail loudly, never silently allow.
        raise ValueError(f"unknown capability: {capability!r}")
    if not has(principal, capability):
        raise PermissionDeniedError(
            f"role {principal.role} does not hold capability {capability}"
        )
