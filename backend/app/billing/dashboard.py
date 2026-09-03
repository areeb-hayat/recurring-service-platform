"""The owner's numbers, computed on the server (P0 §15: ``/dashboard/*``).

Every figure the dashboard shows is derived **here**, from the ledger, by the
same functions statements and the customer page use. The client renders what
this returns and adds nothing up (FIN-4, FIN-11, SYN-9) — which is the whole
reason this module exists rather than a screen summing a page of customer rows.

Two reads:

* :func:`dashboard_summary` — the four §11.1 derivations for the open cycle and
  for the business as a whole, the customer counts, and the most recent payment
  activity.
* :func:`outstanding_customers` — who owes money, largest first, as one grouped
  query rather than one balance read per customer.

**Business generated, billed value, collected and outstanding are four different
numbers** and are never derived from one another; they come from
:mod:`app.billing.reporting`, which separates them by adjustment *origin*. A
voided payment moves collections and outstanding and leaves business generated
alone, on this screen exactly as everywhere else.

**No commission appears here.** Commission is platform scope (COM-7) and a
tenant principal has no capability that reaches it. Operating costs do not
appear either: they are the owner's provider expenses, a separate concept with
its own screen, and mixing them into a customer-revenue summary would produce a
figure that means nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.billing.cycles import open_cycle, serialize_cycle
from app.billing.models import LedgerEntry
from app.billing.reporting import reporting_totals
from app.customers.models import Customer
from app.payments.models import Payment
from app.tenancy.context import TenantContext

__all__ = ["DEFAULT_RECENT_PAYMENTS", "dashboard_summary", "outstanding_customers"]

DEFAULT_RECENT_PAYMENTS = 10
MAX_OUTSTANDING_ROWS = 500


def _customer_balances(ctx: TenantContext):
    """Per-customer outstanding as a subquery — FIN-4, once, in the database."""
    return (
        select(
            LedgerEntry.customer_id.label("customer_id"),
            func.coalesce(func.sum(LedgerEntry.amount_minor), 0).label("outstanding_minor"),
        )
        .where(LedgerEntry.tenant_id == ctx.tenant_id)
        .group_by(LedgerEntry.customer_id)
        .subquery()
    )


def _customer_counts(session: Session, ctx: TenantContext) -> dict[str, int]:
    total, active = session.execute(
        select(
            func.count(),
            func.coalesce(
                func.sum(
                    case((Customer.status == "ACTIVE", 1), else_=0)
                ),
                0,
            ),
        ).where(Customer.tenant_id == ctx.tenant_id)
    ).one()

    balances = _customer_balances(ctx)
    with_balance = session.execute(
        select(func.count())
        .select_from(balances)
        .where(balances.c.outstanding_minor > 0)
    ).scalar_one()
    in_credit = session.execute(
        select(func.count())
        .select_from(balances)
        .where(balances.c.outstanding_minor < 0)
    ).scalar_one()

    return {
        "total": int(total),
        "active": int(active),
        "with_balance_due": int(with_balance),
        "in_credit": int(in_credit),
    }


def _recent_payments(
    session: Session, ctx: TenantContext, limit: int
) -> list[dict[str, Any]]:
    """The latest payment activity, voided rows included (AUD-8).

    A void is exactly the movement an owner most needs to see on a summary
    screen, so hiding it would defeat the purpose. Each row says which it is.
    """
    rows = session.execute(
        select(Payment, Customer.name, Customer.code)
        .join(
            Customer,
            (Customer.tenant_id == Payment.tenant_id)
            & (Customer.id == Payment.customer_id),
        )
        .where(Payment.tenant_id == ctx.tenant_id)
        .order_by(Payment.recorded_at.desc(), Payment.row_version.desc())
        .limit(limit)
    ).all()

    return [
        {
            "id": str(payment.id),
            "customer_id": str(payment.customer_id),
            "customer_name": name,
            "customer_code": code,
            "amount_minor": payment.amount_minor,
            "method": payment.method,
            "received_on": payment.received_on.isoformat(),
            "status": payment.status,
            "reference": payment.reference,
            "recorded_at": payment.recorded_at.isoformat() if payment.recorded_at else None,
        }
        for payment, name, code in rows
    ]


def dashboard_summary(
    session: Session,
    ctx: TenantContext,
    *,
    recent_payments: int = DEFAULT_RECENT_PAYMENTS,
) -> dict[str, Any]:
    """Everything the owner's landing screen shows, in one authoritative read."""
    cycle = open_cycle(session, ctx)

    all_time = reporting_totals(session, ctx)
    cycle_totals = (
        reporting_totals(session, ctx, cycle_id=cycle.id) if cycle is not None else None
    )

    return {
        "business_date": ctx.today.isoformat(),
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
        "unit_label": ctx.unit_label,
        "open_cycle": serialize_cycle(cycle) if cycle is not None else None,
        # FIN-4 over every entry in the tenant: the one definition of a balance.
        "outstanding_minor": all_time.outstanding_minor,
        "all_time": {
            "business_generated_minor": all_time.business_generated_minor,
            "billed_value_minor": all_time.billed_value_minor,
            "collected_minor": all_time.collected_minor,
            "outstanding_minor": all_time.outstanding_minor,
        },
        # Null, not zero, when no cycle is open: there is no current period to
        # report on and a row of zeros would read as "no business this month".
        "current_cycle": (
            {
                "business_generated_minor": cycle_totals.business_generated_minor,
                "billed_value_minor": cycle_totals.billed_value_minor,
                "collected_minor": cycle_totals.collected_minor,
                "outstanding_minor": cycle_totals.outstanding_minor,
            }
            if cycle_totals is not None
            else None
        ),
        "customers": _customer_counts(session, ctx),
        "recent_payments": _recent_payments(session, ctx, max(1, min(recent_payments, 50))),
    }


def outstanding_customers(
    session: Session, ctx: TenantContext, *, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Customers with a non-zero balance, most owed first.

    Credits (a negative balance from an overpayment, FIN-10) are included rather
    than filtered out: money the business is holding is as much a fact about a
    customer as money it is owed, and dropping it would make the page's total
    disagree with :func:`dashboard_summary`.
    """
    limit = max(1, min(limit, MAX_OUTSTANDING_ROWS))
    balances = _customer_balances(ctx)

    rows = session.execute(
        select(
            Customer.id,
            Customer.code,
            Customer.name,
            Customer.area,
            Customer.status,
            balances.c.outstanding_minor,
        )
        .join(balances, balances.c.customer_id == Customer.id)
        .where(Customer.tenant_id == ctx.tenant_id, balances.c.outstanding_minor != 0)
        .order_by(balances.c.outstanding_minor.desc(), Customer.name, Customer.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return {
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
        "items": [
            {
                "customer_id": str(row.id),
                "code": row.code,
                "name": row.name,
                "area": row.area,
                "status": row.status,
                "outstanding_minor": int(row.outstanding_minor),
            }
            for row in rows
        ],
    }
