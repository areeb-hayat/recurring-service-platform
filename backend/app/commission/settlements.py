"""Commission settlement — money actually paid over (COM-6, COM-8, P0 §11).

Independent and additive. A settlement references no earning event, stamps
nothing on one, and rewrites nothing; outstanding is the running aggregate in
:mod:`app.commission.reporting`. That is what makes partial settlement truthful
rather than a special case.

There is no allocation of a settlement to individual events and no
``settlement_id`` anywhere — see P0 §11.1 for why the column was removed rather
than kept nullable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import ActorScope, AuditAction, AuditSource
from app.audit.service import record_audit_event, snapshot
from app.commission.models import CommissionSettlement
from app.core.clock import validate_business_date
from app.core.errors import ValidationFailed
from app.tenancy.context import PlatformContext

__all__ = [
    "RecordSettlementInput",
    "record_settlement",
    "list_settlements",
    "serialize_settlement",
]


@dataclass(frozen=True, slots=True)
class RecordSettlementInput:
    period_start: date
    period_end: date
    amount_minor: int
    settled_on: date | None = None  # None => the tenant's business date
    reference: str | None = None
    note: str | None = None


def list_settlements(session: Session, ctx) -> list[CommissionSettlement]:
    return list(
        session.execute(
            select(CommissionSettlement)
            .where(CommissionSettlement.tenant_id == ctx.tenant_id)
            .order_by(CommissionSettlement.settled_on, CommissionSettlement.created_at)
        )
        .scalars()
        .all()
    )


def serialize_settlement(
    settlement: CommissionSettlement, ctx: PlatformContext
) -> dict[str, Any]:
    return {
        "id": str(settlement.id),
        "tenant_id": str(settlement.tenant_id),
        "period_start": settlement.period_start.isoformat(),
        "period_end": settlement.period_end.isoformat(),
        "amount_minor": settlement.amount_minor,
        "settled_on": settlement.settled_on.isoformat(),
        "reference": settlement.reference,
        "note": settlement.note,
        "created_at": settlement.created_at.isoformat() if settlement.created_at else None,
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
    }


def _validate_amount_minor(value: Any) -> int:
    """Strictly positive, and an integer count of minor units (FIN-1).

    A settlement records money that moved from the tenant to the platform, so a
    zero row says nothing and a negative row is a commission adjustment in
    disguise: it would move outstanding with no snapshotted terms, no link to an
    earning event and no source fact. Commission moves through an adjustment or
    it does not move — that is what makes every movement traceable (COM-4).

    Over-settlement is a different question and stays allowed: settling 1200
    against 1000 earned is a *positive* row that drives outstanding to −200, which
    the frozen aggregate represents exactly (A-COM-6b). A settlement genuinely
    recorded in error is corrected the way this system corrects everything — by
    the platform recording what is actually true next period, not by writing a
    negative payment that never happened.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed(
            "amount_minor must be an int in minor units",
            field_errors={"amount_minor": "must be an integer count of minor units"},
        )
    if value <= 0:
        raise ValidationFailed(
            "amount_minor must be greater than zero",
            field_errors={"amount_minor": "must be greater than zero"},
        )
    return value


def record_settlement(
    session: Session,
    ctx: PlatformContext,
    data: RecordSettlementInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Record one settlement (COM-8: platform scope only).

    No earning row is read, written or annotated. The period is descriptive — it
    names what the platform and the tenant agreed they were settling — and does
    not filter, consume or lock any event.
    """
    amount_minor = _validate_amount_minor(data.amount_minor)
    if data.period_end < data.period_start:
        raise ValidationFailed(
            "period_end must not precede period_start",
            field_errors={"period_end": "must not precede period_start"},
        )
    settled_on = data.settled_on
    if settled_on is None:
        settled_on = ctx.today
    else:
        try:
            settled_on = validate_business_date(
                settled_on, today=ctx.today, field="settled_on"
            )
        except ValueError as exc:
            raise ValidationFailed(
                str(exc), field_errors={"settled_on": str(exc)}
            ) from exc

    settlement = CommissionSettlement(
        tenant_id=ctx.tenant_id,
        period_start=data.period_start,
        period_end=data.period_end,
        amount_minor=amount_minor,
        settled_on=settled_on,
        reference=data.reference,
        note=data.note,
        created_by_user_id=ctx.user_id,
    )
    session.add(settlement)
    session.flush()

    record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_scope=ActorScope.PLATFORM,
        action=AuditAction.COMMISSION_SETTLEMENT_RECORDED,
        entity_type="commission_settlement",
        entity_id=settlement.id,
        before=None,
        after=snapshot("commission_settlement", settlement),
        operation_id=operation_id,
        source=AuditSource.PLATFORM,
    )
    session.flush()
    return serialize_settlement(settlement, ctx), "commission_settlement", settlement.id
