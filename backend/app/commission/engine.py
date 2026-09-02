"""The commission engine: which accepted business facts earn, and by how much.

P0 §11 in one place. Every function here is a *hook* called from the command that
accepts a source business event, inside that command's transaction (COM-2). None
of them is reachable from a client, and there is no route that creates an event
or an automatic adjustment — commission is a consequence of accepted business,
never an authored document.

**Basis decides the trigger** (P0 §11), and each basis follows its §11.1
derivation rather than an ad-hoc sum:

    RECORDED_VALUE   accepted SERVICE charge          (FIN-14)
    BILLED_VALUE     issued statement charges + service adjustments   (FIN-15)
    COLLECTED_VALUE  accepted manual payment          (FIN-16)
    PER_EVENT        a fixed amount per accepted SERVICE record

That is why voiding a payment reverses commission under `COLLECTED_VALUE` and
changes nothing under `RECORDED_VALUE`: the two read different derivations of the
same ledger. A `SKIP` earns under no basis at all — it is a recorded business
fact with no service value and, per P0 §11, `PER_EVENT` pays "per accepted
service record", which a skip is not.

**Terms are snapshotted, never re-read.** An adjustment is always computed from
the terms stored on the original event, so a renegotiated rate cannot reach back
into earned history (COM-3, COM-4).

**One source fact, at most one event.** `(tenant_id, source_type, source_id)` is
unique in the database, and every creation path checks for an existing row first,
so a replayed operation — which `execute_idempotent` already stops before the
command body runs — cannot produce a second event even if it somehow arrived.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.commission.models import (
    CommissionAdjustment,
    CommissionBasis,
    CommissionEvent,
    CommissionPlan,
    CommissionSourceType,
)
from app.commission.plans import effective_plan
from app.core.money import apply_rate_bp
from app.service.models import ServiceKind
from app.tenancy.context import TenantContext

__all__ = [
    "commission_minor_for",
    "on_service_recorded",
    "on_service_corrected",
    "on_service_voided",
    "on_payment_recorded",
    "on_payment_voided",
    "on_statement_issued",
    "find_event",
]


# --- arithmetic --------------------------------------------------------------


def commission_minor_for(
    *,
    basis: str,
    rate_bp: int | None,
    fixed_amount_minor: int | None,
    base_amount_minor: int,
) -> int:
    """Commission for one earning event, in integer minor units (COM-9).

    ``PER_EVENT`` pays its fixed amount regardless of the base; every other basis
    applies its integer rate through the shared half-up rule
    (:func:`app.core.money.apply_rate_bp`). There is no second rounding
    implementation and no float anywhere on this path.
    """
    if basis == CommissionBasis.PER_EVENT:
        assert fixed_amount_minor is not None  # guaranteed by the plan CHECK
        return fixed_amount_minor
    assert rate_bp is not None  # guaranteed by the plan CHECK
    return apply_rate_bp(base_amount_minor, rate_bp)


def _adjustment_minor_for(
    event: CommissionEvent, *, base_delta_minor: int, still_commissionable: bool
) -> int:
    """The signed movement for one correction, void or reversal (COM-4).

    Computed with the event's **original snapshotted terms** — never re-derived
    from today's plan.

    For a rated basis this is the rate applied to the *difference* in base: a
    service reduced from 1000 to 700 moves commission by ``rate x (-300)``. For
    ``PER_EVENT`` the amount never depended on a base, so the only question is
    whether the accepted event still exists: a quantity correction leaves the fee
    intact (0), while a void — or a correction to `SKIP` — removes it entirely.
    """
    if event.basis_snapshot == CommissionBasis.PER_EVENT:
        if still_commissionable:
            return 0
        return -(event.fixed_amount_minor_snapshot or 0)
    return apply_rate_bp(base_delta_minor, event.rate_bp_snapshot)


# --- lookups -----------------------------------------------------------------


def find_event(
    session: Session, ctx, *, source_type: str, source_id: uuid.UUID
) -> CommissionEvent | None:
    return session.execute(
        select(CommissionEvent).where(
            CommissionEvent.tenant_id == ctx.tenant_id,
            CommissionEvent.source_type == source_type,
            CommissionEvent.source_id == source_id,
        )
    ).scalar_one_or_none()


def _chain_ids(session: Session, ctx, record) -> list[uuid.UUID]:
    """The record and every record it corrects, walking ``corrects_id`` back.

    A correction chain earns **once**, at its head: later links post only the
    difference, exactly as the ledger does. Finding the chain's event therefore
    means looking at every link, not just this one.
    """
    from app.service.models import DailyServiceRecord

    ids: list[uuid.UUID] = [record.id]
    corrects_id = record.corrects_id
    seen = {record.id}
    while corrects_id is not None and corrects_id not in seen:
        ids.append(corrects_id)
        seen.add(corrects_id)
        corrects_id = session.execute(
            select(DailyServiceRecord.corrects_id).where(
                DailyServiceRecord.tenant_id == ctx.tenant_id,
                DailyServiceRecord.id == corrects_id,
            )
        ).scalar_one_or_none()
    return ids


def _chain_event(session: Session, ctx, record) -> CommissionEvent | None:
    """The one earning event covering this record's correction chain, if any."""
    return session.execute(
        select(CommissionEvent).where(
            CommissionEvent.tenant_id == ctx.tenant_id,
            CommissionEvent.source_type == CommissionSourceType.DAILY_SERVICE_RECORD,
            CommissionEvent.source_id.in_(_chain_ids(session, ctx, record)),
        )
    ).scalars().first()


# --- writing -----------------------------------------------------------------


def _create_event(
    session: Session,
    ctx: TenantContext,
    plan: CommissionPlan,
    *,
    source_type: str,
    source_id: uuid.UUID,
    base_amount_minor: int,
    occurred_on: date,
) -> CommissionEvent | None:
    """Append one earning event, snapshotting the plan's terms (COM-3).

    Returns ``None`` when an event for this source already exists: one source
    fact earns at most once (COM-5), and the pre-check keeps a stray retry from
    aborting the whole accepting transaction on the unique index.
    """
    if find_event(session, ctx, source_type=source_type, source_id=source_id):
        return None

    event = CommissionEvent(
        tenant_id=ctx.tenant_id,
        plan_id=plan.id,
        basis_snapshot=plan.basis,
        rate_bp_snapshot=plan.rate_bp,
        fixed_amount_minor_snapshot=plan.fixed_amount_minor,
        source_type=source_type,
        source_id=source_id,
        base_amount_minor=base_amount_minor,
        commission_minor=commission_minor_for(
            basis=plan.basis,
            rate_bp=plan.rate_bp,
            fixed_amount_minor=plan.fixed_amount_minor,
            base_amount_minor=base_amount_minor,
        ),
        occurred_on=occurred_on,
    )
    session.add(event)
    session.flush()
    return event


def _create_adjustment(
    session: Session,
    ctx: TenantContext,
    event: CommissionEvent,
    *,
    amount_minor: int,
    reason: str,
    source_type: str,
    source_id: uuid.UUID,
) -> CommissionAdjustment | None:
    """Append one signed adjustment against ``event`` (COM-4).

    Written even when the amount is zero. COM-4 says a correction produces exactly
    one adjustment, and "this correction moved commission by nothing" is a fact
    worth being able to read back — a missing row and a nil effect would otherwise
    be indistinguishable.
    """
    existing = session.execute(
        select(CommissionAdjustment).where(
            CommissionAdjustment.tenant_id == ctx.tenant_id,
            CommissionAdjustment.source_type == source_type,
            CommissionAdjustment.source_id == source_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    adjustment = CommissionAdjustment(
        tenant_id=ctx.tenant_id,
        commission_event_id=event.id,
        amount_minor=amount_minor,
        reason=reason,
        source_type=source_type,
        source_id=source_id,
        created_by_user_id=ctx.user_id,
    )
    session.add(adjustment)
    session.flush()
    return adjustment


# --- triggers ----------------------------------------------------------------


def on_service_recorded(
    session: Session, ctx: TenantContext, record
) -> CommissionEvent | None:
    """An accepted daily service record (RECORDED_VALUE, PER_EVENT).

    A ``SKIP`` earns nothing under any basis: it has no service value, and P0 §11
    pays ``PER_EVENT`` per accepted *service* record. Recording a skip must never
    quietly become billable to the tenant.
    """
    if record.kind != ServiceKind.SERVICE:
        return None
    plan = effective_plan(session, ctx, record.service_date)
    if plan is None or plan.basis not in (
        CommissionBasis.RECORDED_VALUE,
        CommissionBasis.PER_EVENT,
    ):
        return None
    return _create_event(
        session,
        ctx,
        plan,
        source_type=CommissionSourceType.DAILY_SERVICE_RECORD,
        source_id=record.id,
        # For PER_EVENT the fee does not depend on this figure; it is recorded
        # anyway so the row shows what was accepted when the fee was earned.
        base_amount_minor=record.charge_minor,
        occurred_on=record.service_date,
    )


def on_service_corrected(
    session: Session, ctx: TenantContext, *, original, replacement, reason: str
) -> Any:
    """A corrected service record.

    Two cases, and they are not the same thing:

    * the chain already earned — append one adjustment for the difference, at the
      original terms, against the record whose accepted life just ended. That is
      the same source identity the compensating *ledger* entry carries;
    * the chain never earned and the replacement is commissionable — a `SKIP`
      corrected into a `SERVICE` is newly accepted service value, so it earns like
      any other accepted service, under the plan in force for its service date.
    """
    event = _chain_event(session, ctx, original)
    if event is None:
        if replacement.kind == ServiceKind.SERVICE:
            return on_service_recorded(session, ctx, replacement)
        return None

    amount = _adjustment_minor_for(
        event,
        base_delta_minor=replacement.charge_minor - original.charge_minor,
        still_commissionable=replacement.kind == ServiceKind.SERVICE,
    )
    return _create_adjustment(
        session,
        ctx,
        event,
        amount_minor=amount,
        reason=reason,
        source_type=CommissionSourceType.DAILY_SERVICE_RECORD,
        source_id=original.id,
    )


def on_service_voided(
    session: Session, ctx: TenantContext, record, *, reason: str
) -> CommissionAdjustment | None:
    """A voided service record: the accepted value returns to zero."""
    event = _chain_event(session, ctx, record)
    if event is None:
        return None
    amount = _adjustment_minor_for(
        event,
        base_delta_minor=-record.charge_minor,
        still_commissionable=False,
    )
    return _create_adjustment(
        session,
        ctx,
        event,
        amount_minor=amount,
        reason=reason,
        source_type=CommissionSourceType.DAILY_SERVICE_RECORD,
        source_id=record.id,
    )


def on_payment_recorded(
    session: Session, ctx: TenantContext, payment
) -> CommissionEvent | None:
    """An accepted manual payment (COLLECTED_VALUE only)."""
    plan = effective_plan(session, ctx, payment.received_on)
    if plan is None or plan.basis != CommissionBasis.COLLECTED_VALUE:
        return None
    return _create_event(
        session,
        ctx,
        plan,
        source_type=CommissionSourceType.PAYMENT,
        source_id=payment.id,
        base_amount_minor=payment.amount_minor,
        occurred_on=payment.received_on,
    )


def on_payment_voided(
    session: Session, ctx: TenantContext, payment, *, reason: str
) -> CommissionAdjustment | None:
    """A voided payment: collections fall, so collection commission reverses.

    Business-generated commission is untouched, because a `RECORDED_VALUE` plan
    never produced an event for this payment in the first place — the two
    derivations are separated by source, exactly as FIN-14 and FIN-16 are.
    """
    event = find_event(
        session, ctx, source_type=CommissionSourceType.PAYMENT, source_id=payment.id
    )
    if event is None:
        return None
    amount = _adjustment_minor_for(
        event, base_delta_minor=-payment.amount_minor, still_commissionable=False
    )
    return _create_adjustment(
        session,
        ctx,
        event,
        amount_minor=amount,
        reason=reason,
        source_type=CommissionSourceType.PAYMENT,
        source_id=payment.id,
    )


def on_statement_issued(
    session: Session, ctx: TenantContext, statement, cycle
) -> CommissionEvent | None:
    """An issued statement (BILLED_VALUE only).

    The base is FIN-15 exactly — ``charges_minor + service_adjustments_minor`` —
    so a payment reversed inside the cycle never inflates billed value. A
    statement is immutable once issued, so this event can never need an
    adjustment: a later correction is billed on a *later* statement, which earns
    its own.
    """
    plan = effective_plan(session, ctx, cycle.period_end)
    if plan is None or plan.basis != CommissionBasis.BILLED_VALUE:
        return None
    return _create_event(
        session,
        ctx,
        plan,
        source_type=CommissionSourceType.STATEMENT,
        source_id=statement.id,
        base_amount_minor=statement.charges_minor + statement.service_adjustments_minor,
        occurred_on=cycle.period_end,
    )
