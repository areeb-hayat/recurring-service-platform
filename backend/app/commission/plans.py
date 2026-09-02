"""Commission plans — the commercial deal as data (COM-1, P0 §6, §11).

Nothing here decides a rate, a basis or a currency. It stores what the platform
owner configured and resolves which plan was in force on a given business date.
P0 §16 keeps D3 (final rate), D4 (final basis) and D5 (settlement schedule) open
precisely because they are rows, not code.

**One plan per date.** Effective ranges never overlap for a tenant, enforced by an
``EXCLUDE`` constraint in the database. Resolution is therefore a lookup with at
most one answer, not a precedence policy — which matters because those terms are
snapshotted onto immutable earning history, where an ambiguity could never be
corrected afterwards.

**Superseding a plan.** There is no plan-edit route (P0 §15 exposes none). A new
plan starting on date *D* closes the currently open-ended one at *D − 1 day*, in
the same transaction, audited. That is the single permitted mutation of a plan
row: an open range acquiring its end. Terms already snapshotted onto events are
untouched, which is what makes COM-3 and COM-10 hold by construction rather than
by care.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import ActorScope, AuditAction, AuditSource
from app.audit.service import record_audit_event, snapshot
from app.commission.models import CommissionBasis, CommissionPlan
from app.core.errors import CommissionPlanOverlapError, NotFoundError, ValidationFailed
from app.core.money import MoneyError, validate_rate_bp
from app.tenancy.context import PlatformContext

__all__ = [
    "CreatePlanInput",
    "effective_plan",
    "list_plans",
    "load_plan",
    "create_plan",
    "serialize_plan",
]

_OVERLAP_CONSTRAINT = "ex_commission_plan_effective_range_no_overlap"


@dataclass(frozen=True, slots=True)
class CreatePlanInput:
    basis: str
    effective_from: date
    rate_bp: int | None = None
    fixed_amount_minor: int | None = None
    currency: str | None = None  # None => the tenant's own currency
    effective_to: date | None = None


def effective_plan(
    session: Session, ctx, on_date: date
) -> CommissionPlan | None:
    """The plan in force for ``on_date``, or ``None``.

    ``on_date`` is the **business date of the source fact** — the service date, the
    receipt date, the cycle end — not the instant the row happened to be written.
    A delivery made in March and synced in April earns March's terms, which is the
    only reading under which snapshotting means anything: the point of copying the
    terms is to pin them to the business event, and plan ranges are business dates
    for the same reason.

    At most one row can match, guaranteed by the EXCLUDE constraint, so this is a
    lookup and never a choice.
    """
    return session.execute(
        select(CommissionPlan).where(
            CommissionPlan.tenant_id == ctx.tenant_id,
            CommissionPlan.effective_from <= on_date,
            (CommissionPlan.effective_to.is_(None))
            | (CommissionPlan.effective_to >= on_date),
        )
    ).scalar_one_or_none()


def list_plans(session: Session, ctx) -> list[CommissionPlan]:
    return list(
        session.execute(
            select(CommissionPlan)
            .where(CommissionPlan.tenant_id == ctx.tenant_id)
            .order_by(CommissionPlan.effective_from.desc())
        )
        .scalars()
        .all()
    )


def load_plan(session: Session, ctx, plan_id: uuid.UUID) -> CommissionPlan:
    plan = session.execute(
        select(CommissionPlan).where(
            CommissionPlan.tenant_id == ctx.tenant_id, CommissionPlan.id == plan_id
        )
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError("commission plan not found")
    return plan


def serialize_plan(plan: CommissionPlan) -> dict[str, Any]:
    return {
        "id": str(plan.id),
        "tenant_id": str(plan.tenant_id),
        "basis": plan.basis,
        "rate_bp": plan.rate_bp,
        "fixed_amount_minor": plan.fixed_amount_minor,
        "currency": plan.currency,
        "effective_from": plan.effective_from.isoformat(),
        "effective_to": plan.effective_to.isoformat() if plan.effective_to else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


def _validate_terms(data: CreatePlanInput) -> tuple[int | None, int | None]:
    """COM-1/COM-9: exactly one term, and the one the basis actually uses."""
    if data.basis not in CommissionBasis.ALL:
        raise ValidationFailed(
            f"unknown commission basis {data.basis!r}",
            field_errors={"basis": f"must be one of {', '.join(CommissionBasis.ALL)}"},
        )

    if data.basis == CommissionBasis.PER_EVENT:
        if data.fixed_amount_minor is None or data.rate_bp is not None:
            raise ValidationFailed(
                "a PER_EVENT plan is configured with fixed_amount_minor and no rate_bp",
                field_errors={"fixed_amount_minor": "required for PER_EVENT"},
            )
        if isinstance(data.fixed_amount_minor, bool) or not isinstance(
            data.fixed_amount_minor, int
        ):
            raise ValidationFailed(
                "fixed_amount_minor must be an int in minor units",
                field_errors={"fixed_amount_minor": "must be an integer"},
            )
        if data.fixed_amount_minor < 0:
            raise ValidationFailed(
                "fixed_amount_minor must not be negative",
                field_errors={"fixed_amount_minor": "must not be negative"},
            )
        return None, data.fixed_amount_minor

    if data.rate_bp is None or data.fixed_amount_minor is not None:
        raise ValidationFailed(
            f"a {data.basis} plan is configured with rate_bp and no fixed_amount_minor",
            field_errors={"rate_bp": f"required for {data.basis}"},
        )
    try:
        rate_bp = validate_rate_bp(data.rate_bp)
    except MoneyError as exc:
        raise ValidationFailed(str(exc), field_errors={"rate_bp": str(exc)}) from exc
    return rate_bp, None


def _close_predecessor(
    session: Session, ctx: PlatformContext, *, effective_from: date, operation_id
) -> CommissionPlan | None:
    """Close the open-ended plan the new one succeeds, or refuse the overlap.

    The only plan a new one may supersede is an open-ended predecessor that began
    strictly earlier: it acquires ``effective_to = effective_from − 1 day`` and its
    earned history is untouched. Anything else — a plan already carrying an end
    date that covers the new start, or one that begins on or after it — is a real
    conflict, refused rather than silently re-dated.
    """
    clashing = list(
        session.execute(
            select(CommissionPlan).where(
                CommissionPlan.tenant_id == ctx.tenant_id,
                (CommissionPlan.effective_to.is_(None))
                | (CommissionPlan.effective_to >= effective_from),
            )
        )
        .scalars()
        .all()
    )
    if not clashing:
        return None
    if len(clashing) > 1 or clashing[0].effective_from >= effective_from:
        raise CommissionPlanOverlapError(
            "a commission plan already covers "
            f"{effective_from.isoformat()} or later; close it explicitly first",
            extra={"conflicting_plan_ids": sorted(str(p.id) for p in clashing)},
        )

    predecessor = clashing[0]
    if predecessor.effective_to is not None:
        raise CommissionPlanOverlapError(
            f"commission plan {predecessor.id} already ends on "
            f"{predecessor.effective_to.isoformat()} and overlaps "
            f"{effective_from.isoformat()}",
            extra={"conflicting_plan_ids": [str(predecessor.id)]},
        )

    before = snapshot("commission_plan", predecessor)
    predecessor.effective_to = effective_from - timedelta(days=1)
    session.flush()
    record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_scope=ActorScope.PLATFORM,
        action=AuditAction.COMMISSION_PLAN_CLOSED,
        entity_type="commission_plan",
        entity_id=predecessor.id,
        before=before,
        after=snapshot("commission_plan", predecessor),
        reason="superseded by a new commission plan",
        operation_id=operation_id,
        source=AuditSource.PLATFORM,
    )
    return predecessor


def create_plan(
    session: Session,
    ctx: PlatformContext,
    data: CreatePlanInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Create a commission plan (COM-8: platform scope only).

    Existing events, adjustments and settlements are never touched — a plan change
    decides which *future* earning triggers fire and on what terms, and nothing
    else (COM-10).
    """
    rate_bp, fixed_amount_minor = _validate_terms(data)

    currency = (data.currency or ctx.currency).upper()
    if currency != ctx.currency:
        # Not a hard-coded currency: it must simply be the tenant's own, or the
        # plan and the ledger it commissions would be denominated differently.
        raise ValidationFailed(
            f"plan currency {currency} does not match the tenant currency {ctx.currency}",
            field_errors={"currency": f"must be {ctx.currency}"},
        )
    if data.effective_to is not None and data.effective_to < data.effective_from:
        raise ValidationFailed(
            "effective_to must not precede effective_from",
            field_errors={"effective_to": "must not precede effective_from"},
        )

    _close_predecessor(
        session, ctx, effective_from=data.effective_from, operation_id=operation_id
    )

    plan = CommissionPlan(
        tenant_id=ctx.tenant_id,
        basis=data.basis,
        rate_bp=rate_bp,
        fixed_amount_minor=fixed_amount_minor,
        currency=currency,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
        created_by_user_id=ctx.user_id,
    )
    session.add(plan)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # The EXCLUDE constraint is the guarantee; the checks above are the
        # readable message. A race between two platform writers lands here.
        if _OVERLAP_CONSTRAINT not in str(getattr(exc, "orig", exc)):
            raise
        raise CommissionPlanOverlapError(
            "the requested effective range overlaps an existing commission plan"
        ) from exc

    record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_scope=ActorScope.PLATFORM,
        action=AuditAction.COMMISSION_PLAN_CREATED,
        entity_type="commission_plan",
        entity_id=plan.id,
        before=None,
        after=snapshot("commission_plan", plan),
        operation_id=operation_id,
        source=AuditSource.PLATFORM,
    )
    session.flush()
    return serialize_plan(plan), "commission_plan", plan.id
