"""The four reporting derivations and the derived customer status.

P0 §11.1 exists to prevent one specific defect: treating business generated,
billed value, collected and outstanding as interchangeable. They are four
distinct figures read from **one** ledger, separated by adjustment *origin* —
``source_type`` — and never by sign.

    business_generated = Σ CHARGE + Σ ADJUSTMENT WHERE source_type = 'daily_service_record'
    billed_value       = Σ over issued statements of (charges + service_adjustments)
    collected          = − ( Σ PAYMENT + Σ ADJUSTMENT WHERE source_type = 'payment' )
    outstanding        = Σ all ledger entries

The worked example that fixes the rule: a 1000 charge, a 500 payment, then that
payment voided. The void appends a **payment-origin** ADJUSTMENT of +500, so
outstanding correctly returns to 1000 and collected falls back to 0 — while
business generated stays 1000, not 1500. A reversed payment is a collection
event; it neither created nor destroyed service value (FIN-14, FIN-16).

Summing ADJUSTMENT rows by ``entry_kind`` alone, without filtering ``source_type``,
is exactly the bug this module is written to make impossible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.cycles import open_cycle
from app.billing.models import EntryKind, LedgerEntry, SourceType, Statement
from app.tenancy.context import TenantContext

__all__ = [
    "PaymentState",
    "ReportingTotals",
    "business_generated_minor",
    "billed_value_minor",
    "collected_minor",
    "outstanding_total_minor",
    "reporting_totals",
    "customer_payment_status",
]


class PaymentState:
    """FIN-11: derived on every read. There is no stored status column."""

    PAID = "PAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    UNPAID = "UNPAID"


@dataclass(frozen=True, slots=True)
class ReportingTotals:
    business_generated_minor: int
    billed_value_minor: int
    collected_minor: int
    outstanding_minor: int


def _ledger_scope(
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID | None,
    cycle_id: uuid.UUID | None,
) -> list[Any]:
    conditions: list[Any] = [LedgerEntry.tenant_id == ctx.tenant_id]
    if customer_id is not None:
        conditions.append(LedgerEntry.customer_id == customer_id)
    if cycle_id is not None:
        conditions.append(LedgerEntry.posting_cycle_id == cycle_id)
    return conditions


def _ledger_sum(session: Session, conditions: list[Any]) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(*conditions)
        ).scalar_one()
    )


def business_generated_minor(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
) -> int:
    """FIN-14 — what the business actually sold.

    Service-origin adjustments move it; payment-origin adjustments never do.
    """
    scope = _ledger_scope(ctx, customer_id=customer_id, cycle_id=cycle_id)
    charges = _ledger_sum(session, scope + [LedgerEntry.entry_kind == EntryKind.CHARGE])
    service_adjustments = _ledger_sum(
        session,
        scope
        + [
            LedgerEntry.entry_kind == EntryKind.ADJUSTMENT,
            LedgerEntry.source_type == SourceType.DAILY_SERVICE_RECORD,
        ],
    )
    return charges + service_adjustments


def collected_minor(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
) -> int:
    """FIN-16 — money actually received and kept.

    Payment-origin adjustments *are* included, with the opposite effect to
    FIN-14: the same voided payment takes collected back down. Expressed positive.
    """
    scope = _ledger_scope(ctx, customer_id=customer_id, cycle_id=cycle_id)
    payments = _ledger_sum(session, scope + [LedgerEntry.entry_kind == EntryKind.PAYMENT])
    payment_adjustments = _ledger_sum(
        session,
        scope
        + [
            LedgerEntry.entry_kind == EntryKind.ADJUSTMENT,
            LedgerEntry.source_type == SourceType.PAYMENT,
        ],
    )
    return -(payments + payment_adjustments)


def outstanding_total_minor(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
) -> int:
    """FIN-4 — the sum of every ledger entry in scope. No other balance exists."""
    return _ledger_sum(session, _ledger_scope(ctx, customer_id=customer_id, cycle_id=cycle_id))


def billed_value_minor(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
) -> int:
    """FIN-15 — what was actually presented on issued bills.

    Read from **statements**, not from the ledger, and deliberately not derived
    from business generated: service recorded in the currently open cycle is
    generated but not yet billed, and a late correction is billed in a later cycle
    than the one it occurred in. ``payment_reversals_minor`` is excluded for the
    same reason it is excluded from FIN-14.
    """
    conditions: list[Any] = [Statement.tenant_id == ctx.tenant_id]
    if customer_id is not None:
        conditions.append(Statement.customer_id == customer_id)
    if cycle_id is not None:
        conditions.append(Statement.cycle_id == cycle_id)
    return int(
        session.execute(
            select(
                func.coalesce(
                    func.sum(Statement.charges_minor + Statement.service_adjustments_minor),
                    0,
                )
            ).where(*conditions)
        ).scalar_one()
    )


def reporting_totals(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
) -> ReportingTotals:
    """All four figures at once — never one derived from another."""
    return ReportingTotals(
        business_generated_minor=business_generated_minor(
            session, ctx, customer_id=customer_id, cycle_id=cycle_id
        ),
        billed_value_minor=billed_value_minor(
            session, ctx, customer_id=customer_id, cycle_id=cycle_id
        ),
        collected_minor=collected_minor(
            session, ctx, customer_id=customer_id, cycle_id=cycle_id
        ),
        outstanding_minor=outstanding_total_minor(
            session, ctx, customer_id=customer_id, cycle_id=cycle_id
        ),
    )


def customer_payment_status(
    session: Session, ctx: TenantContext, customer_id: uuid.UUID
) -> str:
    """P0 §5.6 / FIN-11 — the single derived status function.

    * ``PAID`` — outstanding ≤ 0 (an overpayment is a credit, not an error)
    * ``UNPAID`` — outstanding > 0 and nothing collected against the current cycle
    * ``PARTIALLY_PAID`` — otherwise

    "Collected against the current cycle" is net of reversals: a payment recorded
    and then voided leaves nothing collected, so the customer is UNPAID again
    rather than stuck at PARTIALLY_PAID on the strength of money that was
    reversed. One function, used identically by every caller — the dashboard,
    statements and the reminder engine must never each have their own.
    """
    outstanding = outstanding_total_minor(session, ctx, customer_id=customer_id)
    if outstanding <= 0:
        return PaymentState.PAID

    cycle = open_cycle(session, ctx)
    if cycle is None:
        return PaymentState.UNPAID

    collected = collected_minor(
        session, ctx, customer_id=customer_id, cycle_id=cycle.id
    )
    return PaymentState.PARTIALLY_PAID if collected != 0 else PaymentState.UNPAID
