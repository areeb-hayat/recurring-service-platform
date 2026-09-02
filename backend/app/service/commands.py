"""Daily service domain commands: record, skip, correct, void.

This is the single domain path every caller uses. P9's voice flow will call
*these functions* after its confirmation step — it gets no path of its own
(VOI-1). ``input_method`` is the only thing that differs, and it is metadata.

Transactional shape: each command performs its lifecycle change, its replacement
or compensation, its ledger posting and its audit event in **one** transaction,
committed by :func:`app.sync.idempotency.execute_idempotent`.

P2 boundary — a correction or void currently posts its adjustment with
``posting_cycle_id = NULL``. P0 §5.5 requires late corrections to post into the
*open* cycle while keeping the original ``occurred_on``. ``occurred_on`` is
already set correctly here (the original service date), so P2 adds cycle
resolution at the single :func:`app.billing.ledger.post_entry` call site without
touching correction semantics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction, AuditSource
from app.audit.service import record_tenant_event, snapshot
from app.billing.ledger import post_service_adjustment, post_service_charge
from app.core.clock import validate_service_date
from app.core.db import next_row_version
from app.core.errors import (
    NotFoundError,
    ServiceAlreadyRecordedError,
    ValidationFailed,
)
from app.core.money import QuantityError, compute_charge_minor, quantize_quantity
from app.customers.models import Customer
from app.service.models import (
    DailyServiceRecord,
    InputMethod,
    RecordStatus,
    ServiceKind,
    Source,
)
from app.tenancy.context import TenantContext

__all__ = [
    "RecordServiceInput",
    "CorrectServiceInput",
    "VoidServiceInput",
    "record_service",
    "correct_service",
    "void_service",
    "serialize_record",
    "load_record",
    "list_day",
]

_ACTIVE_DAY_INDEX = "uq_daily_service_record_active_day"


@dataclass(frozen=True, slots=True)
class RecordServiceInput:
    customer_id: uuid.UUID
    kind: str = ServiceKind.SERVICE
    quantity: Decimal | int | str | None = None
    service_date: date | None = None  # None => the tenant's business date
    input_method: str = InputMethod.BUTTON
    source: str = Source.ONLINE


@dataclass(frozen=True, slots=True)
class CorrectServiceInput:
    quantity: Decimal | int | str | None
    reason: str
    kind: str = ServiceKind.SERVICE
    input_method: str = InputMethod.BUTTON
    source: str = Source.ONLINE


@dataclass(frozen=True, slots=True)
class VoidServiceInput:
    reason: str
    source: str = Source.ONLINE


# --- helpers ----------------------------------------------------------------


def _load_customer(session: Session, ctx: TenantContext, customer_id: uuid.UUID) -> Customer:
    """Tenant-scoped by construction. SEC-4: a foreign id is 404, never 403."""
    customer = session.execute(
        select(Customer).where(
            Customer.tenant_id == ctx.tenant_id, Customer.id == customer_id
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("customer not found")
    if customer.status != "ACTIVE":
        raise ValidationFailed("customer is not active")
    return customer


def load_record(
    session: Session, ctx: TenantContext, record_id: uuid.UUID
) -> DailyServiceRecord:
    record = session.execute(
        select(DailyServiceRecord).where(
            DailyServiceRecord.tenant_id == ctx.tenant_id,
            DailyServiceRecord.id == record_id,
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("service record not found")
    return record


def _resolve_service_date(ctx: TenantContext, requested: date | None) -> date:
    """R4: the server owns "today"; an explicit date is validated separately."""
    if requested is None:
        return ctx.today
    try:
        return validate_service_date(requested, today=ctx.today)
    except ValueError as exc:
        raise ValidationFailed(str(exc), field_errors={"service_date": str(exc)}) from exc


def _resolve_quantity_and_charge(
    kind: str, raw_quantity: Any, unit_price_minor: int
) -> tuple[Decimal, int]:
    """FIN-3/FIN-7: one rounding point; a SKIP is always zero/zero."""
    if kind == ServiceKind.SKIP:
        return Decimal("0.000"), 0
    if raw_quantity is None:
        raise ValidationFailed(
            "quantity is required for a SERVICE record",
            field_errors={"quantity": "required"},
        )
    try:
        quantity = quantize_quantity(raw_quantity)
        charge_minor = compute_charge_minor(quantity, unit_price_minor)
    except (QuantityError, ValueError) as exc:
        raise ValidationFailed(str(exc), field_errors={"quantity": str(exc)}) from exc
    return quantity, charge_minor


def _is_active_day_conflict(exc: IntegrityError) -> bool:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint:
        return constraint == _ACTIVE_DAY_INDEX
    return _ACTIVE_DAY_INDEX in str(exc.orig)


def _flush_or_conflict(session: Session) -> None:
    """Turn the partial unique index violation into a deterministic domain error.

    SYN-4: the *database* is the duplicate-service guarantee. There is no
    pre-read check here on purpose — a SELECT-then-INSERT would race.
    """
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if _is_active_day_conflict(exc):
            raise ServiceAlreadyRecordedError(
                "an active service record already exists for this customer and date"
            ) from exc
        raise


def _require_reason(reason: str | None) -> str:
    """AUD-6: a reason is mandatory on every correction and void."""
    if reason is None or not reason.strip():
        raise ValidationFailed(
            "a reason is required", field_errors={"reason": "required"}
        )
    return reason.strip()


def serialize_record(record: DailyServiceRecord, ctx: TenantContext) -> dict[str, Any]:
    """The stable API/result shape. Money as ``*_minor`` ints, quantity as string."""
    return {
        "id": str(record.id),
        "customer_id": str(record.customer_id),
        "service_date": record.service_date.isoformat(),
        "quantity": str(record.quantity),
        "unit_price_minor": record.unit_price_minor,
        "unit_label": record.unit_label,
        "charge_minor": record.charge_minor,
        "kind": record.kind,
        "status": record.status,
        "corrects_id": str(record.corrects_id) if record.corrects_id else None,
        "superseded_by_id": (
            str(record.superseded_by_id) if record.superseded_by_id else None
        ),
        "adjustment_minor": record.adjustment_minor,
        "reason": record.reason,
        "source": record.source,
        "input_method": record.input_method,
        "operation_id": str(record.operation_id),
        "recorded_at": record.recorded_at.isoformat() if record.recorded_at else None,
        "row_version": record.row_version,
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
    }


# --- commands ---------------------------------------------------------------


def record_service(
    session: Session,
    ctx: TenantContext,
    data: RecordServiceInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Record a SERVICE or SKIP for a customer/day."""
    if data.kind not in (ServiceKind.SERVICE, ServiceKind.SKIP):
        raise ValidationFailed(f"unknown kind {data.kind!r}")

    customer = _load_customer(session, ctx, data.customer_id)
    service_date = _resolve_service_date(ctx, data.service_date)
    quantity, charge_minor = _resolve_quantity_and_charge(
        data.kind, data.quantity, customer.unit_price_minor
    )

    record = DailyServiceRecord(
        tenant_id=ctx.tenant_id,
        customer_id=customer.id,
        service_date=service_date,
        quantity=quantity,
        # FIN-6: snapshot, so a later price change cannot rewrite this charge.
        unit_price_minor=customer.unit_price_minor,
        unit_label=ctx.unit_label,
        charge_minor=charge_minor,
        kind=data.kind,
        status=RecordStatus.ACTIVE,
        recorded_by_user_id=ctx.user_id,
        operation_id=operation_id,
        source=data.source,
        input_method=data.input_method,
        row_version=next_row_version(session),
    )
    session.add(record)
    _flush_or_conflict(session)

    # FIN-7: a SKIP creates the row but posts nothing to the ledger.
    if record.kind == ServiceKind.SERVICE:
        post_service_charge(session, ctx, record)

    record_tenant_event(
        session,
        ctx,
        action=(
            AuditAction.SERVICE_SKIPPED
            if record.kind == ServiceKind.SKIP
            else AuditAction.SERVICE_RECORDED
        ),
        entity_type="daily_service_record",
        entity_id=record.id,
        before=None,
        after=snapshot("daily_service_record", record),
        operation_id=operation_id,
        source=AuditSource.SYNC if data.source == Source.SYNC else AuditSource.ONLINE,
    )
    session.flush()
    return serialize_record(record, ctx), "daily_service_record", record.id


def correct_service(
    session: Session,
    ctx: TenantContext,
    record_id: uuid.UUID,
    data: CorrectServiceInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Supersede an ACTIVE record with a replacement (AUD-2, AUD-3, AUD-4, AUD-5)."""
    reason = _require_reason(data.reason)
    original = load_record(session, ctx, record_id)
    if original.status != RecordStatus.ACTIVE:
        raise ValidationFailed(
            f"only an ACTIVE record can be corrected (this one is {original.status})"
        )
    if data.kind not in (ServiceKind.SERVICE, ServiceKind.SKIP):
        raise ValidationFailed(f"unknown kind {data.kind!r}")

    before = snapshot("daily_service_record", original)

    # FIN-6: the correction re-uses the ORIGINAL snapshotted price. A correction
    # fixes what was delivered, not what it cost; re-pricing at today's rate
    # would silently rewrite history through the back door.
    quantity, charge_minor = _resolve_quantity_and_charge(
        data.kind, data.quantity, original.unit_price_minor
    )
    adjustment_minor = charge_minor - original.charge_minor

    # Free the active slot first, then insert, so the partial unique index sees
    # exactly one ACTIVE row throughout.
    #
    # The superseded row takes its new row_version BEFORE the replacement draws
    # its own, so the replacement is always the LATER value in the shared
    # sequence. A sync cursor that has seen the supersession has therefore
    # necessarily also seen the replacement that explains it; the reverse order
    # would let a client observe an ACTIVE->SUPERSEDED transition with no
    # visible successor.
    original.status = RecordStatus.SUPERSEDED
    original.row_version = next_row_version(session)

    replacement = DailyServiceRecord(
        tenant_id=ctx.tenant_id,
        customer_id=original.customer_id,
        service_date=original.service_date,
        quantity=quantity,
        unit_price_minor=original.unit_price_minor,
        unit_label=original.unit_label,
        charge_minor=charge_minor,
        kind=data.kind,
        status=RecordStatus.ACTIVE,
        corrects_id=original.id,
        adjustment_minor=adjustment_minor,
        reason=reason,
        recorded_by_user_id=ctx.user_id,
        operation_id=operation_id,
        source=data.source,
        input_method=data.input_method,
        row_version=next_row_version(session),
    )
    session.add(replacement)
    session.flush()
    original.superseded_by_id = replacement.id
    _flush_or_conflict(session)

    # The correction posts only the DIFFERENCE (P0 §5.3): the original CHARGE
    # stands untouched, so outstanding lands on the corrected charge.
    #
    # The adjustment is attached to the record whose ACTIVE life is ending, not
    # to the replacement. One uniform rule covers correction and void:
    #
    #     when a record stops being active, post (replacement_charge - its charge)
    #     against THAT record; a void is simply replacement_charge = 0.
    #
    # This also keeps the ledger's (tenant, source_type, source_id, entry_kind)
    # uniqueness satisfiable for a chain of any length: a record is either
    # superseded or voided, never both, so it can only ever carry one
    # ADJUSTMENT. Attaching it to the replacement instead would collide the
    # moment that replacement was later voided.
    post_service_adjustment(
        session,
        ctx,
        customer_id=original.customer_id,
        amount_minor=adjustment_minor,
        occurred_on=original.service_date,
        source_id=original.id,
    )

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.SERVICE_CORRECTED,
        entity_type="daily_service_record",
        entity_id=replacement.id,
        before=before,
        after=snapshot("daily_service_record", replacement),
        reason=reason,
        operation_id=operation_id,
        source=AuditSource.SYNC if data.source == Source.SYNC else AuditSource.ONLINE,
    )
    session.flush()
    return serialize_record(replacement, ctx), "daily_service_record", replacement.id


def void_service(
    session: Session,
    ctx: TenantContext,
    record_id: uuid.UUID,
    data: VoidServiceInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Void an ACTIVE record, appending a compensating adjustment (AUD-1, AUD-2)."""
    reason = _require_reason(data.reason)
    record = load_record(session, ctx, record_id)
    if record.status != RecordStatus.ACTIVE:
        raise ValidationFailed(
            f"only an ACTIVE record can be voided (this one is {record.status})"
        )

    before = snapshot("daily_service_record", record)

    record.status = RecordStatus.VOIDED
    record.reason = reason
    record.row_version = next_row_version(session)
    session.flush()

    # The same rule as a correction with replacement_charge = 0: the net ledger
    # effect of an active record is always its own charge_minor (each earlier
    # correction posted only its difference against the record it replaced), so
    # reversing that amount returns the customer to the pre-record position.
    post_service_adjustment(
        session,
        ctx,
        customer_id=record.customer_id,
        amount_minor=-record.charge_minor,
        occurred_on=record.service_date,
        source_id=record.id,
    )

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.SERVICE_VOIDED,
        entity_type="daily_service_record",
        entity_id=record.id,
        before=before,
        after=snapshot("daily_service_record", record),
        reason=reason,
        operation_id=operation_id,
        source=AuditSource.SYNC if data.source == Source.SYNC else AuditSource.ONLINE,
    )
    session.flush()
    return serialize_record(record, ctx), "daily_service_record", record.id


def list_day(
    session: Session, ctx: TenantContext, service_date: date, *, include_history: bool = False
) -> list[DailyServiceRecord]:
    """Records for one business date.

    AUD-8: ``include_history`` returns superseded and voided rows alongside the
    active one — history stays visible, never hidden.
    """
    stmt = select(DailyServiceRecord).where(
        DailyServiceRecord.tenant_id == ctx.tenant_id,
        DailyServiceRecord.service_date == service_date,
    )
    if not include_history:
        stmt = stmt.where(DailyServiceRecord.status == RecordStatus.ACTIVE)
    stmt = stmt.order_by(DailyServiceRecord.recorded_at)
    return list(session.execute(stmt).scalars().all())
