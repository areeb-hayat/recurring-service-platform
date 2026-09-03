"""Writing audit events.

Append-only: this module offers ``record`` and nothing else. There is no update
and no delete function for :class:`AuditEvent` anywhere in the codebase (AUD-7).

Snapshots are built from an explicit **allow-list** of business fields per entity
type. An allow-list rather than a blacklist because a blacklist silently leaks
whatever someone forgets to add to it — and this table must never contain a
password hash, a refresh token or a JWT.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.audit.models import ActorScope, AuditEvent, AuditSource

__all__ = [
    "record_audit_event",
    "record_tenant_event",
    "record_system_event",
    "snapshot",
    "AUDITABLE_FIELDS",
]

# Only these fields are ever copied into before/after JSON.
AUDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "customer": (
        "code",
        "name",
        "phone_e164",
        "whatsapp_e164",
        "address",
        "area",
        "default_quantity",
        "unit_price_minor",
        "status",
        "row_version",
        # P8: the comparison key travels with the name it belongs to, so an audit
        # reader can see that a rename kept the two in step.
        "normalized_name",
    ),
    "customer_alias": (
        "customer_id",
        "alias",
        "normalized",
        "status",
    ),
    "daily_service_record": (
        "customer_id",
        "service_date",
        "quantity",
        "unit_price_minor",
        "unit_label",
        "charge_minor",
        "kind",
        "status",
        "corrects_id",
        "superseded_by_id",
        "adjustment_minor",
        "reason",
        "source",
        "input_method",
    ),
    "payment": (
        "customer_id",
        "amount_minor",
        "method",
        "received_on",
        "reference",
        "note",
        "status",
        "voided_reason",
        "source",
    ),
    "billing_cycle": ("period_start", "period_end", "status"),
    "commission_plan": (
        "basis",
        "rate_bp",
        "fixed_amount_minor",
        "currency",
        "effective_from",
        "effective_to",
    ),
    "commission_settlement": (
        "period_start",
        "period_end",
        "amount_minor",
        "settled_on",
        "reference",
        "note",
    ),
    "app_user": ("email", "role", "status"),
    "reminder": (
        "customer_id",
        "cycle_id",
        "schedule_day",
        "kind",
        "amount_minor_at_generation",
        "state",
        "attempt_count",
        "last_error",
        "sent_at",
        "cancelled_at",
    ),
    "job_run": ("kind", "business_date", "status", "triggered_by", "detail"),
    "operating_cost_item": ("code", "name", "description", "status"),
    "operating_cost_rate": (
        "cost_item_id",
        "effective_from",
        "effective_to",
        "unit",
        "unit_price_minor",
        "fixed_amount_minor",
        "fixed_recurrence",
        "currency",
        "currency_exponent",
        "source_note",
    ),
    "operating_cost_usage": (
        "cost_item_id",
        "rate_id",
        "period_month",
        "usage_quantity",
        "usage_unit",
        "unit_price_minor_snapshot",
        "estimated_amount_minor",
        "currency",
        "inputs",
        "note",
        "status",
        "supersedes_id",
        "superseded_by_id",
        "correction_reason",
    ),
    "operating_cost_actual": (
        "cost_item_id",
        "period_month",
        "amount_minor",
        "currency",
        "invoice_reference",
        "note",
        "status",
        "supersedes_id",
        "superseded_by_id",
        "correction_reason",
    ),
}

# Belt and braces: even if an allow-list entry is mistyped, these never serialize.
_FORBIDDEN_FIELDS = frozenset(
    {"password", "password_hash", "refresh_token", "refresh_token_hash", "token", "secret"}
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)  # never float — FIN-1
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def snapshot(entity_type: str, entity: Any) -> dict[str, Any] | None:
    """Build a JSON-safe snapshot of the auditable fields of ``entity``."""
    if entity is None:
        return None
    fields = AUDITABLE_FIELDS.get(entity_type)
    if fields is None:
        raise ValueError(f"no audit allow-list defined for entity type {entity_type!r}")
    out: dict[str, Any] = {}
    for field in fields:
        if field in _FORBIDDEN_FIELDS:  # pragma: no cover - guarded by test
            raise ValueError(f"field {field!r} must never be audited")
        out[field] = _jsonable(getattr(entity, field, None))
    return out


def record_audit_event(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    actor_scope: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    operation_id: uuid.UUID | None = None,
    request_id: str | None = None,
    source: str = AuditSource.ONLINE,
) -> AuditEvent:
    """Append one audit event. Never commits — the caller owns the transaction."""
    event = AuditEvent(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_scope=actor_scope,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
        operation_id=operation_id,
        request_id=request_id,
        source=source,
    )
    session.add(event)
    return event


def record_tenant_event(session: Session, ctx, **kwargs) -> AuditEvent:
    """Convenience wrapper for the common tenant-scoped case."""
    return record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_scope=ActorScope.TENANT,
        **kwargs,
    )


def record_system_event(session: Session, ctx, **kwargs) -> AuditEvent:
    """A scheduled job's audit row (AUD-9).

    ``actor_user_id`` is NULL and the scope is ``SYSTEM``, because the cron is
    not a person. Provenance is preserved rather than faked: a reader can always
    tell a reminder the runner sent from one an owner re-dispatched by hand.
    """
    kwargs.setdefault("source", AuditSource.JOB)
    return record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_user_id=None,
        actor_scope=ActorScope.SYSTEM,
        **kwargs,
    )
