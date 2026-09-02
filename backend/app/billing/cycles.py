"""Billing cycles: period arithmetic, the open cycle, and close (P0 §5.5).

Three rules, and nothing beyond them:

* Exactly one ``OPEN`` cycle per tenant, guaranteed by a partial unique index.
* Periods are never shortened: a cycle always runs its full configured length.
* ``period_end`` is **inclusive**, so a cycle may not be closed until it has
  passed — the earliest valid close is ``business_date > period_end``. Events
  dated on ``period_end`` therefore stay eligible for the cycle all day, whatever
  time somebody tries to close it.
* An OPEN cycle whose period has ended accepts nothing: the write fails closed
  asking for the rollover, rather than filing today's business under a period
  that is over.
* Closing is one-way, there is no reopen and no override flag, and there is
  deliberately **no period locking** beyond it — P0 §11.1 ends with "no period
  locking beyond cycle close", so a *backdated* record into a closed period is
  still accepted and simply posts to the open cycle (§5.5).

The cycle is created lazily, on the first posting that needs one, rather than by a
scheduled job: P2 has no job runner, and a cycle that exists only because someone
recorded business is exactly as correct as one created at midnight.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction, AuditSource
from app.audit.service import record_tenant_event, snapshot
from app.billing.models import BillingCycle, CycleStatus
from app.core.errors import (
    CyclePeriodNotEndedError,
    CycleRolloverRequiredError,
    NotFoundError,
    ValidationFailed,
)
from app.tenancy.context import TenantContext

__all__ = [
    "MONTHLY",
    "period_bounds",
    "ensure_open_cycle",
    "open_cycle",
    "load_cycle",
    "list_cycles",
    "close_cycle",
    "serialize_cycle",
]

MONTHLY = "MONTHLY"

_ONE_OPEN_INDEX = "uq_billing_cycle_one_open_per_tenant"


def _add_one_month(day: date) -> date:
    """Same day-of-month, next month.

    Safe without clamping because ``tenant.cycle_start_day`` is constrained to
    1..28 in the database — a cycle that starts on the 31st has no meaning in
    February, which is why the column was bounded rather than handled here.
    """
    if day.month == 12:
        return date(day.year + 1, 1, day.day)
    return date(day.year, day.month + 1, day.day)


def period_bounds(ctx: TenantContext, on_date: date) -> tuple[date, date]:
    """The tenant-local period containing ``on_date``.

    Monthly is the frozen V1 default (P0 §16). Weekly and fortnightly are deferred
    decision D7 behind ``tenant.cycle_type``; an unimplemented type fails closed
    rather than silently behaving like a month.
    """
    if ctx.cycle_type != MONTHLY:
        raise ValidationFailed(
            f"cycle_type {ctx.cycle_type!r} is not implemented in V1 "
            "(P0 deferred decision D7); only MONTHLY is supported"
        )
    start_day = ctx.cycle_start_day
    if on_date.day >= start_day:
        start = date(on_date.year, on_date.month, start_day)
    else:
        first_of_month = date(on_date.year, on_date.month, 1)
        previous = first_of_month - timedelta(days=1)
        start = date(previous.year, previous.month, start_day)
    return start, _add_one_month(start) - timedelta(days=1)


def open_cycle(session: Session, ctx: TenantContext) -> BillingCycle | None:
    return session.execute(
        select(BillingCycle).where(
            BillingCycle.tenant_id == ctx.tenant_id,
            BillingCycle.status == CycleStatus.OPEN,
        )
    ).scalar_one_or_none()


def ensure_open_cycle(session: Session, ctx: TenantContext) -> BillingCycle:
    """The tenant's *valid* open cycle, creating it if none exists.

    "Valid" is the whole point: an OPEN cycle whose ``period_end`` is already
    past does **not** qualify. Returning it would post today's business into a
    period that has ended — an August cycle left open on 1 September silently
    swallowing September's service — which is a mis-stated bill rather than a
    late one. That case raises :class:`CycleRolloverRequiredError` instead.

    A cycle created here is always the full configured period containing today.
    It cannot collide with an existing one: an OPEN cycle was returned above, and
    a CLOSED cycle covering today is impossible because closing requires
    ``business_date > period_end`` and time only moves forward.

    Backdating is unaffected. A record dated inside an *earlier*, already closed
    period still posts here, to the current open cycle, keeping its true
    ``occurred_on`` (P0 §5.5). The rule enforced is one-directional: an entry may
    post to a cycle that started before it, never to one that ended before it.
    """
    existing = open_cycle(session, ctx)
    if existing is not None:
        if existing.period_end < ctx.today:
            raise CycleRolloverRequiredError(
                f"the open billing cycle {existing.period_start.isoformat()}.."
                f"{existing.period_end.isoformat()} ended before the tenant business "
                f"date {ctx.today.isoformat()}; close it before recording new business",
                extra={
                    "cycle_id": str(existing.id),
                    "period_end": existing.period_end.isoformat(),
                    "business_date": ctx.today.isoformat(),
                },
            )
        return existing

    start, end = period_bounds(ctx, ctx.today)

    cycle = BillingCycle(
        tenant_id=ctx.tenant_id,
        period_start=start,
        period_end=end,
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    try:
        session.flush()
    except IntegrityError as exc:
        # Another transaction opened it first. The partial unique index is the
        # serialization point, exactly as it is for the daily-record active day;
        # there is no pre-read to race with.
        if _ONE_OPEN_INDEX not in str(getattr(exc, "orig", exc)):
            raise
        session.rollback()
        winner = open_cycle(session, ctx)
        if winner is None:  # pragma: no cover - only if the winner rolled back
            raise
        return winner
    return cycle


def load_cycle(session: Session, ctx: TenantContext, cycle_id: uuid.UUID) -> BillingCycle:
    cycle = session.execute(
        select(BillingCycle).where(
            BillingCycle.tenant_id == ctx.tenant_id, BillingCycle.id == cycle_id
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise NotFoundError("billing cycle not found")
    return cycle


def list_cycles(session: Session, ctx: TenantContext) -> list[BillingCycle]:
    return list(
        session.execute(
            select(BillingCycle)
            .where(BillingCycle.tenant_id == ctx.tenant_id)
            .order_by(BillingCycle.period_start.desc())
        )
        .scalars()
        .all()
    )


def serialize_cycle(cycle: BillingCycle) -> dict[str, Any]:
    return {
        "id": str(cycle.id),
        "period_start": cycle.period_start.isoformat(),
        "period_end": cycle.period_end.isoformat(),
        "status": cycle.status,
        "closed_at": cycle.closed_at.isoformat() if cycle.closed_at else None,
    }


def close_cycle(
    session: Session,
    ctx: TenantContext,
    cycle_id: uuid.UUID,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Close an OPEN cycle whose period has ended, and issue its statements.

    Issue happens here rather than through a route of its own because a statement
    is only sound once its cycle can receive no further entries: P0 §15 exposes no
    statement-issuing endpoint, and issuing from an open cycle would let a later
    posting contradict a document FIN-8 declares immutable.

    ``period_end`` is inclusive, so the earliest valid close is the day after it;
    closing on or before it is refused — see
    :class:`~app.core.errors.CyclePeriodNotEndedError`.
    """
    from app.billing.statements import issue_statements_for_cycle

    cycle = load_cycle(session, ctx, cycle_id)
    if cycle.status != CycleStatus.OPEN:
        raise ValidationFailed(
            f"only an OPEN cycle can be closed (this one is {cycle.status})"
        )
    # period_end is INCLUSIVE, so the period is still running throughout that day
    # and events dated on it must stay eligible for this cycle. The earliest valid
    # close is the day after. Evaluated against the tenant-local business date the
    # server derived, never a date the caller supplied.
    if ctx.today <= cycle.period_end:
        raise CyclePeriodNotEndedError(
            f"billing cycle {cycle.period_start.isoformat()}..{cycle.period_end.isoformat()} "
            f"cannot be closed until after {cycle.period_end.isoformat()} "
            f"(tenant business date is {ctx.today.isoformat()})",
            extra={
                "period_end": cycle.period_end.isoformat(),
                "business_date": ctx.today.isoformat(),
            },
        )

    before = snapshot("billing_cycle", cycle)
    cycle.status = CycleStatus.CLOSED
    cycle.closed_at = ctx.now
    cycle.closed_by_user_id = ctx.user_id
    session.flush()

    statements = issue_statements_for_cycle(session, ctx, cycle)

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.BILLING_CYCLE_CLOSED,
        entity_type="billing_cycle",
        entity_id=cycle.id,
        before=before,
        after=snapshot("billing_cycle", cycle),
        operation_id=operation_id,
        source=AuditSource.ONLINE,
    )
    session.flush()

    result = serialize_cycle(cycle)
    result["statements_issued"] = len(statements)
    return result, "billing_cycle", cycle.id
