"""Manual payment domain commands: record and void (PAY-1..PAY-9).

The whole V1 payment engine. It has no provider, no gateway, no callback and no
externally verified state, and it is complete without one (FIN-13): full, partial
and over-payment all behave, and voiding works, on nothing but this table and the
ledger.

Both commands run through :func:`app.sync.idempotency.execute_idempotent` at the
API layer, so a payment gets the *same* duplicate protection as every other write
(PAY-5, PAY-8) — there is no payment-specific dedupe mechanism, and deliberately
no amount/date natural key (PAY-6).

Voice cannot reach either command (PAY-9, VOI-7): there is no voice write path in
the product at all, and the operational intent schema cannot express a payment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditAction, AuditSource
from app.audit.service import record_tenant_event, snapshot
from app.billing.ledger import post_payment, post_payment_adjustment
from app.core.clock import validate_business_date
from app.core.db import next_row_version
from app.core.errors import NotFoundError, ValidationFailed
from app.customers.models import Customer
from app.payments.models import Payment, PaymentMethod, PaymentStatus
from app.service.models import Source
from app.tenancy.context import TenantContext

__all__ = [
    "RecordPaymentInput",
    "VoidPaymentInput",
    "record_payment",
    "void_payment",
    "load_payment",
    "list_payments",
    "serialize_payment",
]


@dataclass(frozen=True, slots=True)
class RecordPaymentInput:
    customer_id: uuid.UUID
    amount_minor: int
    method: str = PaymentMethod.CASH
    received_on: date | None = None  # None => the tenant's business date
    reference: str | None = None
    note: str | None = None
    source: str = Source.ONLINE


@dataclass(frozen=True, slots=True)
class VoidPaymentInput:
    reason: str
    source: str = Source.ONLINE


def _load_customer(session: Session, ctx: TenantContext, customer_id: uuid.UUID) -> Customer:
    """Tenant-scoped by construction. SEC-4/PAY-4: a foreign id is 404."""
    customer = session.execute(
        select(Customer).where(
            Customer.tenant_id == ctx.tenant_id, Customer.id == customer_id
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("customer not found")
    return customer


def load_payment(session: Session, ctx: TenantContext, payment_id: uuid.UUID) -> Payment:
    payment = session.execute(
        select(Payment).where(
            Payment.tenant_id == ctx.tenant_id, Payment.id == payment_id
        )
    ).scalar_one_or_none()
    if payment is None:
        raise NotFoundError("payment not found")
    return payment


def list_payments(
    session: Session, ctx: TenantContext, customer_id: uuid.UUID
) -> list[Payment]:
    """Both RECORDED and VOIDED rows: history stays visible (AUD-8)."""
    return list(
        session.execute(
            select(Payment)
            .where(Payment.tenant_id == ctx.tenant_id, Payment.customer_id == customer_id)
            .order_by(Payment.received_on, Payment.recorded_at)
        )
        .scalars()
        .all()
    )


def _validate_amount_minor(value: Any) -> int:
    """PAY-3. Also enforced by a database CHECK — this is the readable message."""
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


def _resolve_received_on(ctx: TenantContext, requested: date | None) -> date:
    """The server owns "today"; an explicit date is validated separately (R4)."""
    if requested is None:
        return ctx.today
    try:
        return validate_business_date(requested, today=ctx.today, field="received_on")
    except ValueError as exc:
        raise ValidationFailed(str(exc), field_errors={"received_on": str(exc)}) from exc


def _require_reason(reason: str | None) -> str:
    """AUD-6: a reason is mandatory on every void."""
    if reason is None or not reason.strip():
        raise ValidationFailed("a reason is required", field_errors={"reason": "required"})
    return reason.strip()


def serialize_payment(payment: Payment, ctx: TenantContext) -> dict[str, Any]:
    return {
        "id": str(payment.id),
        "customer_id": str(payment.customer_id),
        "amount_minor": payment.amount_minor,
        "method": payment.method,
        "received_on": payment.received_on.isoformat(),
        "reference": payment.reference,
        "note": payment.note,
        "status": payment.status,
        "voided_reason": payment.voided_reason,
        "voided_at": payment.voided_at.isoformat() if payment.voided_at else None,
        "operation_id": str(payment.operation_id),
        "source": payment.source,
        "recorded_at": payment.recorded_at.isoformat() if payment.recorded_at else None,
        "row_version": payment.row_version,
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
    }


def record_payment(
    session: Session,
    ctx: TenantContext,
    data: RecordPaymentInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Record one manual payment and post its PAYMENT ledger entry.

    Any positive amount is accepted, including one larger than the outstanding
    balance: an overpayment yields a negative (credit) balance rather than an
    error (FIN-10). The server never clamps a payment to what is owed — that would
    silently discard money the owner actually received.
    """
    if data.method not in PaymentMethod.ALL:
        raise ValidationFailed(
            f"unknown payment method {data.method!r}",
            field_errors={"method": f"must be one of {', '.join(PaymentMethod.ALL)}"},
        )

    customer = _load_customer(session, ctx, data.customer_id)
    amount_minor = _validate_amount_minor(data.amount_minor)
    received_on = _resolve_received_on(ctx, data.received_on)

    payment = Payment(
        tenant_id=ctx.tenant_id,
        customer_id=customer.id,
        amount_minor=amount_minor,
        method=data.method,
        received_on=received_on,
        reference=data.reference,
        note=data.note,
        status=PaymentStatus.RECORDED,
        operation_id=operation_id,
        recorded_by_user_id=ctx.user_id,
        source=data.source,
        row_version=next_row_version(session),
    )
    session.add(payment)
    session.flush()

    post_payment(session, ctx, payment)

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.PAYMENT_RECORDED,
        entity_type="payment",
        entity_id=payment.id,
        before=None,
        after=snapshot("payment", payment),
        operation_id=operation_id,
        source=AuditSource.SYNC if data.source == Source.SYNC else AuditSource.ONLINE,
    )
    session.flush()
    return serialize_payment(payment, ctx), "payment", payment.id


def void_payment(
    session: Session,
    ctx: TenantContext,
    payment_id: uuid.UUID,
    data: VoidPaymentInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Void a RECORDED payment (PAY-7, AUD-1, AUD-2, AUD-3, AUD-6).

    The payment row survives as ``VOIDED`` carrying its reason, actor and
    timestamp; its amount is never edited and it is never deleted. The reversal is
    a compensating **payment-origin** ADJUSTMENT, which is what returns outstanding
    and collections to their pre-payment values while leaving business generated
    untouched (FIN-14).

    The compensating entry keeps the payment's original ``received_on`` but posts
    to the currently OPEN cycle (§5.5), so voiding a payment that was billed on a
    delivered statement never rewrites that statement.

    The transition advances the payment's ``row_version`` — it is the only
    permitted mutation of the row, and a client holding payment history offline
    must see it on the next delta (P0 §7.1, §7.4).
    """
    reason = _require_reason(data.reason)
    payment = load_payment(session, ctx, payment_id)
    if payment.status != PaymentStatus.RECORDED:
        raise ValidationFailed(
            f"only a RECORDED payment can be voided (this one is {payment.status})"
        )

    before = snapshot("payment", payment)

    payment.status = PaymentStatus.VOIDED
    payment.voided_reason = reason
    payment.voided_by_user_id = ctx.user_id
    payment.voided_at = ctx.now
    # The voided row takes its new version BEFORE the compensating entry draws
    # its own, so the entry always sorts later — the same ordering rule P1 fixed
    # for corrections. A cursor that has seen the void has necessarily also seen
    # the ledger movement that explains it.
    payment.row_version = next_row_version(session)
    session.flush()

    post_payment_adjustment(
        session,
        ctx,
        customer_id=payment.customer_id,
        amount_minor=payment.amount_minor,
        occurred_on=payment.received_on,
        source_id=payment.id,
    )

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.PAYMENT_VOIDED,
        entity_type="payment",
        entity_id=payment.id,
        before=before,
        after=snapshot("payment", payment),
        reason=reason,
        operation_id=operation_id,
        source=AuditSource.SYNC if data.source == Source.SYNC else AuditSource.ONLINE,
    )
    session.flush()
    return serialize_payment(payment, ctx), "payment", payment.id
