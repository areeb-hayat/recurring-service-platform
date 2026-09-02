"""Shared helpers for the P3 commission suites.

Plans and settlements are created the way the routes create them — through
:func:`app.sync.idempotency.execute_idempotent` with a
:class:`~app.tenancy.context.PlatformContext` — so these tests exercise the real
authority boundary and the real transaction, not a shortcut around either.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.commission.models import (
    CommissionAdjustment,
    CommissionEvent,
    CommissionPlan,
    CommissionSettlement,
)
from app.commission.plans import CreatePlanInput, create_plan
from app.commission.reporting import commission_position
from app.commission.settlements import RecordSettlementInput, record_settlement
from app.core.clock import FixedClock
from app.core.ids import uuid7
from app.identity.models import Role
from app.sync.idempotency import execute_idempotent
from app.tenancy.context import PlatformContext, Principal

# A date comfortably before the frozen "today" (2026-03-15 in Asia/Karachi), so a
# plan effective from here covers everything the suites record.
EARLY = date(2026, 1, 1)


def platform_ctx(fixture, platform_user, clock) -> PlatformContext:
    """Platform authority over one explicitly named tenant."""
    return PlatformContext.build(
        principal=Principal(
            user_id=platform_user.id,
            role=Role.PLATFORM_OWNER,
            scope="PLATFORM",
            tenant_id=None,
        ),
        tenant=fixture.tenant,
        clock=clock if isinstance(clock, FixedClock) else clock,
    )


def _run(db: Session, ctx, op_type: str, payload: dict, perform):
    operation_id = payload.pop("_operation_id", None) or uuid7()
    return execute_idempotent(
        db,
        ctx,
        operation_id=operation_id,
        op_type=op_type,
        payload={**payload, "_op": str(operation_id)},
        perform=lambda: perform(operation_id),
    )


def make_plan(
    db: Session,
    pctx: PlatformContext,
    *,
    basis: str,
    rate_bp: int | None = None,
    fixed_amount_minor: int | None = None,
    effective_from: date = EARLY,
    effective_to: date | None = None,
    operation_id: uuid.UUID | None = None,
):
    data = CreatePlanInput(
        basis=basis,
        rate_bp=rate_bp,
        fixed_amount_minor=fixed_amount_minor,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    return _run(
        db,
        pctx,
        "commission.plan.create",
        {"basis": basis, "from": effective_from.isoformat(), "_operation_id": operation_id},
        lambda op: create_plan(db, pctx, data, operation_id=op),
    )


def make_settlement(
    db: Session,
    pctx: PlatformContext,
    amount_minor: int,
    *,
    period_start: date = EARLY,
    period_end: date = date(2026, 12, 31),
    settled_on: date | None = None,
    operation_id: uuid.UUID | None = None,
):
    data = RecordSettlementInput(
        period_start=period_start,
        period_end=period_end,
        amount_minor=amount_minor,
        settled_on=settled_on,
    )
    return _run(
        db,
        pctx,
        "commission.settlement.record",
        {"amount_minor": amount_minor, "_operation_id": operation_id},
        lambda op: record_settlement(db, pctx, data, operation_id=op),
    )


def events(db: Session, ctx) -> list[CommissionEvent]:
    return list(
        db.execute(
            select(CommissionEvent)
            .where(CommissionEvent.tenant_id == ctx.tenant_id)
            .order_by(CommissionEvent.created_at, CommissionEvent.id)
        )
        .scalars()
        .all()
    )


def adjustments(db: Session, ctx) -> list[CommissionAdjustment]:
    return list(
        db.execute(
            select(CommissionAdjustment)
            .where(CommissionAdjustment.tenant_id == ctx.tenant_id)
            .order_by(CommissionAdjustment.created_at, CommissionAdjustment.id)
        )
        .scalars()
        .all()
    )


def settlements(db: Session, ctx) -> list[CommissionSettlement]:
    return list(
        db.execute(
            select(CommissionSettlement)
            .where(CommissionSettlement.tenant_id == ctx.tenant_id)
            .order_by(CommissionSettlement.created_at, CommissionSettlement.id)
        )
        .scalars()
        .all()
    )


def plans(db: Session, ctx) -> list[CommissionPlan]:
    return list(
        db.execute(
            select(CommissionPlan)
            .where(CommissionPlan.tenant_id == ctx.tenant_id)
            .order_by(CommissionPlan.effective_from)
        )
        .scalars()
        .all()
    )


def outstanding(db: Session, ctx) -> int:
    return commission_position(db, ctx).outstanding_minor


def snapshot_rows(rows) -> list[dict]:
    """Every column of every row, for asserting nothing was touched (A-COM-6)."""
    return [
        {c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows
    ]
