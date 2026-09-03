"""Operating-cost writes: items, versioned rates, measured usage, real invoices.

Every command here goes through the ordinary
:func:`app.sync.idempotency.execute_idempotent` register at the API layer, so an
operating-cost write gets the same replay protection as every other write — and
none of them is an accepted sync operation, because P6 keeps cost mutations
online-only (P6 §19).

**Nothing in this module touches the customer ledger or commission.** It imports
neither. An operating cost is money the business owes a provider; it can never
change what a customer owes or what the platform earned.

**Corrections, not edits** (AUD-1, AUD-2, AUD-3, AUD-6). A usage figure or an
invoice amount that turns out to be wrong is replaced by appending a new ACTIVE
row and marking the old one SUPERSEDED with a mandatory reason, the actor and
the timestamp — the same shape a corrected daily service record has. There is no
update of an accepted amount and no delete path anywhere in the file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction, AuditSource
from app.audit.service import record_tenant_event, snapshot
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.ids import new_id
from app.costs.estimates import RateTerms, effective_rate, estimate_minor, month_start
from app.costs.models import (
    USAGE_QUANTITY_SCALE,
    CostItemStatus,
    CostRecurrence,
    CostRowStatus,
    OperatingCostActual,
    OperatingCostItem,
    OperatingCostRate,
    OperatingCostUsage,
)
from app.tenancy.context import TenantContext

__all__ = [
    "CreateCostItemInput",
    "CreateCostRateInput",
    "RecordUsageInput",
    "RecordActualInput",
    "create_cost_item",
    "create_cost_rate",
    "record_usage",
    "record_actual",
    "list_cost_items",
    "list_rates",
    "load_cost_item",
    "active_usage",
    "active_actual",
    "serialize_cost_item",
    "serialize_rate",
    "serialize_usage",
    "serialize_actual",
]

_RATE_OVERLAP_CONSTRAINT = "ex_operating_cost_rate_effective_range_no_overlap"
_USAGE_ACTIVE_INDEX = "uq_operating_cost_usage_active_period"
_ACTUAL_ACTIVE_INDEX = "uq_operating_cost_actual_active_period"

_QUANTITY_EXPONENT = Decimal(1).scaleb(-USAGE_QUANTITY_SCALE)
# NUMERIC(18,6): twelve digits ahead of the point.
_USAGE_MAX = Decimal("999999999999.999999")


# --- inputs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateCostItemInput:
    code: str
    name: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class CreateCostRateInput:
    cost_item_id: uuid.UUID
    effective_from: date
    unit: str | None = None
    unit_price_minor: int | None = None
    fixed_amount_minor: int | None = None
    fixed_recurrence: str | None = None
    currency: str | None = None  # None => the tenant's own currency
    currency_exponent: int | None = None
    source_note: str | None = None


@dataclass(frozen=True, slots=True)
class RecordUsageInput:
    cost_item_id: uuid.UUID
    period_month: date
    usage_quantity: Decimal
    inputs: dict[str, Any] | None = None
    note: str | None = None
    #: Required only when replacing a month that already has a figure (AUD-6).
    correction_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RecordActualInput:
    cost_item_id: uuid.UUID
    period_month: date
    amount_minor: int
    currency: str | None = None
    currency_exponent: int | None = None
    invoice_reference: str | None = None
    note: str | None = None
    correction_reason: str | None = None


# --- reads -------------------------------------------------------------------


def load_cost_item(
    session: Session, ctx: TenantContext, cost_item_id: uuid.UUID
) -> OperatingCostItem:
    """Tenant-scoped by construction. SEC-4: a foreign id is 404."""
    item = session.execute(
        select(OperatingCostItem).where(
            OperatingCostItem.tenant_id == ctx.tenant_id,
            OperatingCostItem.id == cost_item_id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError("operating cost item not found")
    return item


def list_cost_items(
    session: Session, ctx: TenantContext, *, include_archived: bool = False
) -> list[OperatingCostItem]:
    conditions = [OperatingCostItem.tenant_id == ctx.tenant_id]
    if not include_archived:
        conditions.append(OperatingCostItem.status == CostItemStatus.ACTIVE)
    return list(
        session.execute(
            select(OperatingCostItem).where(*conditions).order_by(OperatingCostItem.code)
        )
        .scalars()
        .all()
    )


def list_rates(
    session: Session, ctx: TenantContext, *, cost_item_id: uuid.UUID | None = None
) -> list[OperatingCostRate]:
    """Every rate, newest range first. Nothing is hidden: an old rate is the
    only explanation an old estimate has."""
    conditions = [OperatingCostRate.tenant_id == ctx.tenant_id]
    if cost_item_id is not None:
        conditions.append(OperatingCostRate.cost_item_id == cost_item_id)
    return list(
        session.execute(
            select(OperatingCostRate)
            .where(*conditions)
            .order_by(
                OperatingCostRate.cost_item_id, OperatingCostRate.effective_from.desc()
            )
        )
        .scalars()
        .all()
    )


def active_usage(
    session: Session, ctx: TenantContext, *, cost_item_id: uuid.UUID, period_month: date
) -> OperatingCostUsage | None:
    return session.execute(
        select(OperatingCostUsage).where(
            OperatingCostUsage.tenant_id == ctx.tenant_id,
            OperatingCostUsage.cost_item_id == cost_item_id,
            OperatingCostUsage.period_month == period_month,
            OperatingCostUsage.status == CostRowStatus.ACTIVE,
        )
    ).scalar_one_or_none()


def active_actual(
    session: Session, ctx: TenantContext, *, cost_item_id: uuid.UUID, period_month: date
) -> OperatingCostActual | None:
    return session.execute(
        select(OperatingCostActual).where(
            OperatingCostActual.tenant_id == ctx.tenant_id,
            OperatingCostActual.cost_item_id == cost_item_id,
            OperatingCostActual.period_month == period_month,
            OperatingCostActual.status == CostRowStatus.ACTIVE,
        )
    ).scalar_one_or_none()


# --- serializers -------------------------------------------------------------


def serialize_cost_item(item: OperatingCostItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "code": item.code,
        "name": item.name,
        "description": item.description,
        "status": item.status,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def serialize_rate(rate: OperatingCostRate) -> dict[str, Any]:
    return {
        "id": str(rate.id),
        "cost_item_id": str(rate.cost_item_id),
        "effective_from": rate.effective_from.isoformat(),
        "effective_to": rate.effective_to.isoformat() if rate.effective_to else None,
        "unit": rate.unit,
        "unit_price_minor": rate.unit_price_minor,
        "fixed_amount_minor": rate.fixed_amount_minor,
        "fixed_recurrence": rate.fixed_recurrence,
        "currency": rate.currency,
        "currency_exponent": rate.currency_exponent,
        "source_note": rate.source_note,
        "created_at": rate.created_at.isoformat() if rate.created_at else None,
    }


def serialize_usage(row: OperatingCostUsage) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "cost_item_id": str(row.cost_item_id),
        "rate_id": str(row.rate_id),
        "period_month": row.period_month.isoformat(),
        # A string, for the same reason a service quantity is: never a JSON float.
        "usage_quantity": str(row.usage_quantity),
        "usage_unit": row.usage_unit,
        "unit_price_minor_snapshot": row.unit_price_minor_snapshot,
        "estimated_amount_minor": row.estimated_amount_minor,
        "currency": row.currency,
        "currency_exponent": row.currency_exponent,
        "inputs": row.inputs,
        "note": row.note,
        "status": row.status,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "superseded_by_id": str(row.superseded_by_id) if row.superseded_by_id else None,
        "correction_reason": row.correction_reason,
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
    }


def serialize_actual(row: OperatingCostActual) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "cost_item_id": str(row.cost_item_id),
        "period_month": row.period_month.isoformat(),
        "amount_minor": row.amount_minor,
        "currency": row.currency,
        "currency_exponent": row.currency_exponent,
        "invoice_reference": row.invoice_reference,
        "note": row.note,
        "status": row.status,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "superseded_by_id": str(row.superseded_by_id) if row.superseded_by_id else None,
        "correction_reason": row.correction_reason,
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
    }


# --- validation helpers ------------------------------------------------------


def _require_month(value: date, ctx: TenantContext, field: str = "period_month") -> date:
    """The first day of a month that has actually begun.

    Usage is *measured* and an invoice is *received*, so neither can belong to a
    month that has not started. Planning ahead is what the scenario calculator is
    for, and it writes nothing.
    """
    month = month_start(value)
    if month > month_start(ctx.today):
        raise ValidationFailed(
            f"{field} {month.isoformat()} is in the future; "
            "usage and invoices are recorded for months that have begun",
            field_errors={field: "must not be a future month"},
        )
    return month


def _require_reason(reason: str | None, *, what: str) -> str:
    if reason is None or not reason.strip():
        raise ValidationFailed(
            f"a reason is required to replace an existing {what}",
            field_errors={"correction_reason": "required"},
        )
    return reason.strip()


def _quantize_usage(value: Decimal | int | str) -> Decimal:
    """Exact, bounded, and never a float (FIN-1 applies to every amount here)."""
    if isinstance(value, float):
        raise ValidationFailed(
            "usage_quantity must not be a float; send a decimal string",
            field_errors={"usage_quantity": "must be a decimal string"},
        )
    if isinstance(value, bool):
        raise ValidationFailed(
            "usage_quantity must be a number",
            field_errors={"usage_quantity": "must be a number"},
        )
    try:
        quantity = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationFailed(
            f"usage_quantity is not a valid decimal: {value!r}",
            field_errors={"usage_quantity": "must be a decimal string"},
        ) from exc
    if not quantity.is_finite() or quantity < 0:
        raise ValidationFailed(
            "usage_quantity must be a non-negative number",
            field_errors={"usage_quantity": "must not be negative"},
        )
    if quantity > _USAGE_MAX:
        raise ValidationFailed(
            "usage_quantity is larger than the column can store",
            field_errors={"usage_quantity": "too large"},
        )
    quantized = quantity.quantize(_QUANTITY_EXPONENT)
    if quantized != quantity:
        raise ValidationFailed(
            f"usage_quantity carries more than {USAGE_QUANTITY_SCALE} decimal places",
            field_errors={"usage_quantity": f"at most {USAGE_QUANTITY_SCALE} decimals"},
        )
    return quantized


def _validate_money_minor(value: Any, field: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed(
            f"{field} must be an int in minor units",
            field_errors={field: "must be an integer count of minor units"},
        )
    if value < 0 or (value == 0 and not allow_zero):
        raise ValidationFailed(
            f"{field} must not be negative",
            field_errors={field: "must not be negative"},
        )
    return value


# --- writes ------------------------------------------------------------------


def _flush_one_active(session: Session, index_name: str, what: str) -> None:
    """Flush, turning a lost race for a month's ACTIVE row into a conflict.

    Two different ``operation_id``s recording the same month at the same instant
    are two different requests, so idempotency does not merge them: the partial
    unique index decides, and the loser is told so rather than raising a 500.
    """
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if index_name not in str(getattr(exc, "orig", exc)):
            raise
        raise ConflictError(
            f"another {what} for that month was recorded first; reload and try again",
            code="COST_PERIOD_CONFLICT",
        ) from exc


def create_cost_item(
    session: Session,
    ctx: TenantContext,
    data: CreateCostItemInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Add a provider / cost line. The vocabulary is the owner's, not the code's."""
    code = (data.code or "").strip().upper()
    name = (data.name or "").strip()
    if not code or not name:
        raise ValidationFailed(
            "a cost item needs a code and a name",
            field_errors={
                **({} if code else {"code": "required"}),
                **({} if name else {"name": "required"}),
            },
        )

    item = OperatingCostItem(
        tenant_id=ctx.tenant_id,
        code=code,
        name=name,
        description=(data.description or None),
        status=CostItemStatus.ACTIVE,
        created_by_user_id=ctx.user_id,
    )
    session.add(item)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if "uq_operating_cost_item_tenant_id_code" not in str(getattr(exc, "orig", exc)):
            raise
        raise ConflictError(
            f"a cost item with code {code!r} already exists",
            code="COST_ITEM_CODE_TAKEN",
            field_errors={"code": "already in use"},
        ) from exc

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.OPERATING_COST_ITEM_CREATED,
        entity_type="operating_cost_item",
        entity_id=item.id,
        before=None,
        after=snapshot("operating_cost_item", item),
        operation_id=operation_id,
        source=AuditSource.ONLINE,
    )
    session.flush()
    return serialize_cost_item(item), "operating_cost_item", item.id


def _close_predecessor_rate(
    session: Session,
    ctx: TenantContext,
    *,
    cost_item_id: uuid.UUID,
    effective_from: date,
    operation_id: uuid.UUID,
) -> None:
    """Close the open-ended rate the new one succeeds, or refuse the overlap.

    Exactly the ``commission_plan`` rule, for exactly the same reason: a rate's
    terms are snapshotted onto usage rows, so an ambiguous "rate in force" would
    be baked into history nobody can correct. The only rate a new one may
    supersede is an open-ended predecessor that began strictly earlier; it
    acquires ``effective_to = effective_from - 1 day`` and its recorded estimates
    are untouched.
    """
    clashing = list(
        session.execute(
            select(OperatingCostRate).where(
                OperatingCostRate.tenant_id == ctx.tenant_id,
                OperatingCostRate.cost_item_id == cost_item_id,
                (OperatingCostRate.effective_to.is_(None))
                | (OperatingCostRate.effective_to >= effective_from),
            )
        )
        .scalars()
        .all()
    )
    if not clashing:
        return
    if (
        len(clashing) > 1
        or clashing[0].effective_from >= effective_from
        or clashing[0].effective_to is not None
    ):
        raise ConflictError(
            f"a rate already covers {effective_from.isoformat()} or later for this "
            "cost item; close it explicitly first",
            code="COST_RATE_OVERLAP",
            extra={"conflicting_rate_ids": sorted(str(r.id) for r in clashing)},
        )

    predecessor = clashing[0]
    before = snapshot("operating_cost_rate", predecessor)
    predecessor.effective_to = effective_from - timedelta(days=1)
    session.flush()
    record_tenant_event(
        session,
        ctx,
        action=AuditAction.OPERATING_COST_RATE_CLOSED,
        entity_type="operating_cost_rate",
        entity_id=predecessor.id,
        before=before,
        after=snapshot("operating_cost_rate", predecessor),
        reason="superseded by a new rate",
        operation_id=operation_id,
        source=AuditSource.ONLINE,
    )


def create_cost_rate(
    session: Session,
    ctx: TenantContext,
    data: CreateCostRateInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Add a versioned rate. The previous open-ended one is closed, never edited."""
    item = load_cost_item(session, ctx, data.cost_item_id)

    usage_priced = data.unit_price_minor is not None
    fixed = data.fixed_amount_minor is not None
    if usage_priced == fixed:
        raise ValidationFailed(
            "a rate is either usage priced (unit + unit_price_minor) or fixed "
            "(fixed_amount_minor + fixed_recurrence) — exactly one",
            field_errors={"unit_price_minor": "set exactly one pricing shape"},
        )

    unit = (data.unit or "").strip() or None
    recurrence = data.fixed_recurrence
    if usage_priced:
        if unit is None:
            raise ValidationFailed(
                "a usage-priced rate needs the unit it is priced per",
                field_errors={"unit": "required"},
            )
        if recurrence is not None:
            raise ValidationFailed(
                "fixed_recurrence belongs to a fixed rate, not a usage-priced one",
                field_errors={"fixed_recurrence": "not valid for a usage-priced rate"},
            )
        _validate_money_minor(data.unit_price_minor, "unit_price_minor")
    else:
        if recurrence not in CostRecurrence.ALL:
            raise ValidationFailed(
                "a fixed rate needs a recurrence",
                field_errors={
                    "fixed_recurrence": f"must be one of {', '.join(CostRecurrence.ALL)}"
                },
            )
        _validate_money_minor(data.fixed_amount_minor, "fixed_amount_minor")

    # Provider prices are routinely quoted in a currency the tenant does not bill
    # in, and V1 has no FX source (P6 §18), so the currency is carried, never
    # converted. Defaulting to the tenant's own currency is the ordinary case.
    currency = (data.currency or ctx.currency).upper()
    if len(currency) != 3:
        raise ValidationFailed(
            "currency must be a 3-letter code",
            field_errors={"currency": "must be 3 letters"},
        )
    exponent = (
        data.currency_exponent
        if data.currency_exponent is not None
        else (ctx.currency_exponent if currency == ctx.currency else 2)
    )
    if not isinstance(exponent, int) or isinstance(exponent, bool) or not 0 <= exponent <= 4:
        raise ValidationFailed(
            "currency_exponent must be between 0 and 4",
            field_errors={"currency_exponent": "must be between 0 and 4"},
        )

    _close_predecessor_rate(
        session,
        ctx,
        cost_item_id=item.id,
        effective_from=data.effective_from,
        operation_id=operation_id,
    )

    rate = OperatingCostRate(
        tenant_id=ctx.tenant_id,
        cost_item_id=item.id,
        effective_from=data.effective_from,
        # Always open-ended. A rate ends when its successor begins, closed by
        # `_close_predecessor_rate` above — there is no way to write a range that
        # leaves a gap with no rate in force.
        effective_to=None,
        unit=unit,
        unit_price_minor=data.unit_price_minor,
        fixed_amount_minor=data.fixed_amount_minor,
        fixed_recurrence=recurrence,
        currency=currency,
        currency_exponent=exponent,
        source_note=(data.source_note or None),
        created_by_user_id=ctx.user_id,
    )
    session.add(rate)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if _RATE_OVERLAP_CONSTRAINT not in str(getattr(exc, "orig", exc)):
            raise
        raise ConflictError(
            "that effective range overlaps an existing rate for this cost item",
            code="COST_RATE_OVERLAP",
        ) from exc

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.OPERATING_COST_RATE_CREATED,
        entity_type="operating_cost_rate",
        entity_id=rate.id,
        before=None,
        after=snapshot("operating_cost_rate", rate),
        operation_id=operation_id,
        source=AuditSource.ONLINE,
    )
    session.flush()
    return serialize_rate(rate), "operating_cost_rate", rate.id


def record_usage(
    session: Session,
    ctx: TenantContext,
    data: RecordUsageInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Record one month's measured usage and freeze the estimate it produces.

    The rate's terms are copied onto the row. That is what makes a later rate
    change leave this month alone: the estimate is not re-derived on read, it is
    what was computed from the terms in force at the time.
    """
    item = load_cost_item(session, ctx, data.cost_item_id)
    period = _require_month(data.period_month, ctx)
    quantity = _quantize_usage(data.usage_quantity)

    rate = effective_rate(session, ctx, cost_item_id=item.id, on_date=period)
    if rate is None:
        raise ValidationFailed(
            f"no rate is in force for {item.code} in {period.isoformat()[:7]}; "
            "add the rate that applied before recording usage for it",
            field_errors={"cost_item_id": "no rate in force for that month"},
        )
    if rate.unit_price_minor is None:
        raise ValidationFailed(
            f"{item.code} is on a fixed rate for {period.isoformat()[:7]}; "
            "its monthly cost is the rate itself and needs no usage figure",
            field_errors={"cost_item_id": "fixed-rate items take no usage"},
        )

    terms = RateTerms.of(rate)
    estimated = estimate_minor(terms, quantity)
    assert estimated is not None  # a usage-priced rate with a quantity always yields one

    previous = active_usage(
        session, ctx, cost_item_id=item.id, period_month=period
    )
    reason: str | None = None
    if previous is not None:
        # AUD-2/AUD-6: the old figure is superseded with a reason, never edited.
        reason = _require_reason(data.correction_reason, what="usage figure")

    # The successor's id is drawn up front so the outgoing row can be closed in
    # ONE statement — status, reason and successor together. Splitting it would
    # momentarily write a SUPERSEDED row with no successor, which the
    # ``superseded_requires_reason`` CHECK correctly refuses. And the close must
    # come *before* the insert regardless: the partial unique index allows only
    # one ACTIVE row per (item, month), so the slot has to be freed first.
    row_id = new_id()
    before = None
    if previous is not None:
        before = snapshot("operating_cost_usage", previous)
        previous.status = CostRowStatus.SUPERSEDED
        previous.correction_reason = reason
        previous.superseded_by_id = row_id
        session.flush()

    row = OperatingCostUsage(
        id=row_id,
        tenant_id=ctx.tenant_id,
        cost_item_id=item.id,
        rate_id=rate.id,
        period_month=period,
        usage_quantity=quantity,
        usage_unit=rate.unit or "",
        unit_price_minor_snapshot=rate.unit_price_minor,
        estimated_amount_minor=estimated,
        currency=rate.currency,
        currency_exponent=rate.currency_exponent,
        inputs=data.inputs,
        note=(data.note or None),
        status=CostRowStatus.ACTIVE,
        supersedes_id=previous.id if previous else None,
        recorded_by_user_id=ctx.user_id,
    )
    session.add(row)
    _flush_one_active(session, _USAGE_ACTIVE_INDEX, "usage figure")

    record_tenant_event(
        session,
        ctx,
        action=(
            AuditAction.OPERATING_COST_USAGE_CORRECTED
            if previous is not None
            else AuditAction.OPERATING_COST_USAGE_RECORDED
        ),
        entity_type="operating_cost_usage",
        entity_id=row.id,
        before=before,
        after=snapshot("operating_cost_usage", row),
        reason=reason,
        operation_id=operation_id,
        source=AuditSource.ONLINE,
    )
    session.flush()
    return serialize_usage(row), "operating_cost_usage", row.id


def record_actual(
    session: Session,
    ctx: TenantContext,
    data: RecordActualInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Record what a provider actually invoiced for a month.

    Independent of the estimate on purpose. The invoice is the fact; the estimate
    was the expectation; the difference between them is the number the owner
    actually wants, and it only means something if neither is derived from the
    other.
    """
    item = load_cost_item(session, ctx, data.cost_item_id)
    period = _require_month(data.period_month, ctx)
    amount = _validate_money_minor(data.amount_minor, "amount_minor")

    # Default to the currency of the rate that applied, then to the tenant's —
    # the provider bills in its own currency and nothing here converts.
    rate = effective_rate(session, ctx, cost_item_id=item.id, on_date=period)
    currency = (
        data.currency.upper()
        if data.currency
        else (rate.currency if rate is not None else ctx.currency)
    )
    if len(currency) != 3:
        raise ValidationFailed(
            "currency must be a 3-letter code",
            field_errors={"currency": "must be 3 letters"},
        )
    if data.currency_exponent is not None:
        exponent = data.currency_exponent
    elif rate is not None and rate.currency == currency:
        exponent = rate.currency_exponent
    elif currency == ctx.currency:
        exponent = ctx.currency_exponent
    else:
        exponent = 2
    if not isinstance(exponent, int) or isinstance(exponent, bool) or not 0 <= exponent <= 4:
        raise ValidationFailed(
            "currency_exponent must be between 0 and 4",
            field_errors={"currency_exponent": "must be between 0 and 4"},
        )

    previous = active_actual(session, ctx, cost_item_id=item.id, period_month=period)
    reason: str | None = None
    if previous is not None:
        reason = _require_reason(data.correction_reason, what="invoice amount")

    # Same ordering as the usage path, for the same two reasons: the CHECK wants
    # the outgoing row closed in one statement, and the partial unique index
    # wants the ACTIVE slot free before the successor is inserted.
    row_id = new_id()
    before = None
    if previous is not None:
        before = snapshot("operating_cost_actual", previous)
        previous.status = CostRowStatus.SUPERSEDED
        previous.correction_reason = reason
        previous.superseded_by_id = row_id
        session.flush()

    row = OperatingCostActual(
        id=row_id,
        tenant_id=ctx.tenant_id,
        cost_item_id=item.id,
        period_month=period,
        amount_minor=amount,
        currency=currency,
        currency_exponent=exponent,
        invoice_reference=(data.invoice_reference or None),
        note=(data.note or None),
        status=CostRowStatus.ACTIVE,
        supersedes_id=previous.id if previous else None,
        recorded_by_user_id=ctx.user_id,
    )
    session.add(row)
    _flush_one_active(session, _ACTUAL_ACTIVE_INDEX, "invoice")

    record_tenant_event(
        session,
        ctx,
        action=(
            AuditAction.OPERATING_COST_ACTUAL_CORRECTED
            if previous is not None
            else AuditAction.OPERATING_COST_ACTUAL_RECORDED
        ),
        entity_type="operating_cost_actual",
        entity_id=row.id,
        before=before,
        after=snapshot("operating_cost_actual", row),
        reason=reason,
        operation_id=operation_id,
        source=AuditSource.ONLINE,
    )
    session.flush()
    return serialize_actual(row), "operating_cost_actual", row.id
