"""Statement issue and carry-forward (P0 §5.4, §5.5; FIN-8, FIN-15).

A statement is a frozen presentation of one cycle's slice of the ledger:

    closing = opening + charges + service_adjustments - payments + payment_reversals

Every figure is a sum of already-rounded integers (FIN-3): nothing here re-rounds,
and nothing here recomputes a quantity times a price.

Carry-forward needs no transfer entry. The ledger is continuous per customer, so
the next statement's opening balance *is* the previous statement's closing
balance — and where no previous statement exists, it is the sum of everything
posted to earlier cycles, which is the same number by construction.

Statements are issued when a cycle closes and are immutable from that instant
(FIN-8). Nothing in this module updates one; the database rejects the attempt
regardless.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.models import (
    BillingCycle,
    EntryKind,
    LedgerEntry,
    SourceType,
    Statement,
)
from app.commission import engine as commission
from app.core.db import next_row_version
from app.core.errors import NotFoundError, ValidationFailed
from app.service.models import DailyServiceRecord
from app.tenancy.context import TenantContext

__all__ = [
    "CycleMovements",
    "cycle_movements",
    "issue_statements_for_cycle",
    "load_statement",
    "list_statements",
    "serialize_statement",
]


@dataclass(frozen=True, slots=True)
class CycleMovements:
    """The five movement figures for one customer in one cycle.

    ``payments`` and ``payment_reversals`` are expressed **positive**, matching
    the statement columns; the ledger stores a payment negative.
    """

    charges_minor: int
    service_adjustments_minor: int
    payments_minor: int
    payment_reversals_minor: int
    service_days: int
    total_quantity: Decimal


def _sum(session: Session, *conditions) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(*conditions)
        ).scalar_one()
    )


def cycle_movements(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID,
    cycle_id: uuid.UUID,
) -> CycleMovements:
    """Movements for one customer over the entries posted to one cycle.

    Adjustments are split by ``source_type``, never by sign: a −500 service
    correction and a +500 payment reversal are different columns even though both
    are ADJUSTMENT rows (P0 §5.3).
    """
    scope = (
        LedgerEntry.tenant_id == ctx.tenant_id,
        LedgerEntry.customer_id == customer_id,
        LedgerEntry.posting_cycle_id == cycle_id,
    )

    charges = _sum(session, *scope, LedgerEntry.entry_kind == EntryKind.CHARGE)
    service_adjustments = _sum(
        session,
        *scope,
        LedgerEntry.entry_kind == EntryKind.ADJUSTMENT,
        LedgerEntry.source_type == SourceType.DAILY_SERVICE_RECORD,
    )
    payments_signed = _sum(session, *scope, LedgerEntry.entry_kind == EntryKind.PAYMENT)
    payment_reversals = _sum(
        session,
        *scope,
        LedgerEntry.entry_kind == EntryKind.ADJUSTMENT,
        LedgerEntry.source_type == SourceType.PAYMENT,
    )

    # Presentation figures derived from the same entries as charges_minor, so the
    # two can never disagree: the days and units this cycle actually billed.
    stats = session.execute(
        select(
            func.count(func.distinct(LedgerEntry.occurred_on)),
            func.coalesce(func.sum(DailyServiceRecord.quantity), 0),
        )
        .select_from(LedgerEntry)
        .join(
            DailyServiceRecord,
            (DailyServiceRecord.tenant_id == LedgerEntry.tenant_id)
            & (DailyServiceRecord.id == LedgerEntry.source_id),
        )
        .where(*scope, LedgerEntry.entry_kind == EntryKind.CHARGE)
    ).one()

    return CycleMovements(
        charges_minor=charges,
        service_adjustments_minor=service_adjustments,
        # Stored positive; the ledger holds them negative.
        payments_minor=-payments_signed,
        payment_reversals_minor=payment_reversals,
        service_days=int(stats[0]),
        total_quantity=Decimal(stats[1]),
    )


def _previous_statement(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID,
    period_start,
) -> Statement | None:
    return (
        session.execute(
            select(Statement)
            .join(
                BillingCycle,
                (BillingCycle.tenant_id == Statement.tenant_id)
                & (BillingCycle.id == Statement.cycle_id),
            )
            .where(
                Statement.tenant_id == ctx.tenant_id,
                Statement.customer_id == customer_id,
                BillingCycle.period_start < period_start,
            )
            .order_by(BillingCycle.period_start.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _opening_balance_minor(
    session: Session, ctx: TenantContext, *, customer_id: uuid.UUID, cycle: BillingCycle
) -> int:
    """Carry-forward (FIN-8): the previous statement's closing balance.

    With no previous statement the same number is derived from the ledger — the
    sum of everything posted to earlier cycles. The two agree by construction,
    because an entry only ever posts to the open (latest) cycle, so a cycle that
    has been closed can never gain another entry.
    """
    previous = _previous_statement(
        session, ctx, customer_id=customer_id, period_start=cycle.period_start
    )
    if previous is not None:
        return previous.closing_balance_minor

    return _sum(
        session,
        LedgerEntry.tenant_id == ctx.tenant_id,
        LedgerEntry.customer_id == customer_id,
        LedgerEntry.posting_cycle_id.in_(
            select(BillingCycle.id).where(
                BillingCycle.tenant_id == ctx.tenant_id,
                BillingCycle.period_start < cycle.period_start,
            )
        ),
    )


def _billable_customers(
    session: Session, ctx: TenantContext, cycle: BillingCycle
) -> list[uuid.UUID]:
    """Every customer this cycle must bill.

    Includes customers with no movement this cycle but a balance carried in: they
    still receive a statement, whose closing equals its opening. Excluded are
    customers who have never had a ledger entry at all — a statement of five zeros
    is not a document anyone wants.
    """
    rows = (
        session.execute(
            select(LedgerEntry.customer_id)
            .distinct()
            .where(
                LedgerEntry.tenant_id == ctx.tenant_id,
                LedgerEntry.posting_cycle_id.in_(
                    select(BillingCycle.id).where(
                        BillingCycle.tenant_id == ctx.tenant_id,
                        BillingCycle.period_start <= cycle.period_start,
                    )
                ),
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _reject_unassigned_entries(session: Session, ctx: TenantContext) -> None:
    """Fail closed if any entry belongs to no cycle.

    P1 wrote every entry with ``posting_cycle_id = NULL`` because ``billing_cycle``
    did not exist yet. Such a row would be silently omitted from both the opening
    balance and the movements, producing a statement that is wrong rather than
    late — so issuing refuses while one exists instead of guessing which cycle it
    belonged to.
    """
    orphaned = session.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(
            LedgerEntry.tenant_id == ctx.tenant_id,
            LedgerEntry.posting_cycle_id.is_(None),
        )
    ).scalar_one()
    if orphaned:
        raise ValidationFailed(
            f"{orphaned} ledger entries have no posting cycle; "
            "assign them before issuing statements"
        )


def issue_statements_for_cycle(
    session: Session, ctx: TenantContext, cycle: BillingCycle
) -> list[Statement]:
    """Issue one immutable statement per billable customer for ``cycle``."""
    _reject_unassigned_entries(session, ctx)

    issued: list[Statement] = []
    for customer_id in _billable_customers(session, ctx, cycle):
        movements = cycle_movements(
            session, ctx, customer_id=customer_id, cycle_id=cycle.id
        )
        opening = _opening_balance_minor(
            session, ctx, customer_id=customer_id, cycle=cycle
        )
        # The §5.4 identity, computed once here and re-asserted by a CHECK on insert.
        closing = (
            opening
            + movements.charges_minor
            + movements.service_adjustments_minor
            - movements.payments_minor
            + movements.payment_reversals_minor
        )
        statement = Statement(
            tenant_id=ctx.tenant_id,
            customer_id=customer_id,
            cycle_id=cycle.id,
            opening_balance_minor=opening,
            charges_minor=movements.charges_minor,
            service_adjustments_minor=movements.service_adjustments_minor,
            payments_minor=movements.payments_minor,
            payment_reversals_minor=movements.payment_reversals_minor,
            closing_balance_minor=closing,
            service_days=movements.service_days,
            total_quantity=movements.total_quantity,
            unit_label=ctx.unit_label,
            currency=ctx.currency,
            # Drawn once, at issue. An immutable row never needs a second value.
            row_version=next_row_version(session),
        )
        session.add(statement)
        issued.append(statement)
    session.flush()

    # FIN-15 is what a BILLED_VALUE plan earns on, so commission is created here,
    # at issue, in the close transaction — not when the service was recorded.
    for statement in issued:
        commission.on_statement_issued(session, ctx, statement, cycle)
    return issued


def load_statement(
    session: Session, ctx: TenantContext, statement_id: uuid.UUID
) -> Statement:
    statement = session.execute(
        select(Statement).where(
            Statement.tenant_id == ctx.tenant_id, Statement.id == statement_id
        )
    ).scalar_one_or_none()
    if statement is None:
        raise NotFoundError("statement not found")
    return statement


def list_statements(
    session: Session, ctx: TenantContext, customer_id: uuid.UUID
) -> list[Statement]:
    return list(
        session.execute(
            select(Statement)
            .join(
                BillingCycle,
                (BillingCycle.tenant_id == Statement.tenant_id)
                & (BillingCycle.id == Statement.cycle_id),
            )
            .where(
                Statement.tenant_id == ctx.tenant_id,
                Statement.customer_id == customer_id,
            )
            .order_by(BillingCycle.period_start.desc())
        )
        .scalars()
        .all()
    )


def serialize_statement(statement: Statement, ctx: TenantContext) -> dict[str, Any]:
    return {
        "id": str(statement.id),
        "customer_id": str(statement.customer_id),
        "cycle_id": str(statement.cycle_id),
        "issued_at": statement.issued_at.isoformat() if statement.issued_at else None,
        "opening_balance_minor": statement.opening_balance_minor,
        "charges_minor": statement.charges_minor,
        "service_adjustments_minor": statement.service_adjustments_minor,
        "payments_minor": statement.payments_minor,
        "payment_reversals_minor": statement.payment_reversals_minor,
        "closing_balance_minor": statement.closing_balance_minor,
        "service_days": statement.service_days,
        "total_quantity": str(statement.total_quantity),
        "unit_label": statement.unit_label,
        "currency": statement.currency,
        "currency_exponent": ctx.currency_exponent,
        "row_version": statement.row_version,
    }
