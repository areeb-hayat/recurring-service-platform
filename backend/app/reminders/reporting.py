"""What the owner sees on the reminders screen.

**Every number here is the server's** (P0 §3, SYN-9). The outstanding shown
beside a customer is :func:`app.billing.ledger.outstanding_minor` — the same
authoritative figure the reminder engine itself sends — not the reminder's stored
``amount_minor_at_generation`` and not something a client added up. That is the
whole point of the screen: it must answer "what would go out if a reminder went
out now", and after a payment that is a different number from what last went out.

**The per-customer state is derived here, not in the browser.** The client
renders and filters on a string this module produced; it never decides whether a
customer is due, settled or needs attention, because those decisions come from
the same schedule and eligibility rules the engine uses and duplicating them in
TypeScript would be a second implementation to drift.

Read-only. Nothing in this module writes, and nothing in it can send a reminder.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.models import BillingCycle, LedgerEntry, Statement
from app.core.errors import NotFoundError
from app.customers.models import Customer
from app.reminders.engine import serialize_reminder
from app.reminders.models import (
    CommunicationLog,
    OUTSTANDING_KINDS,
    Reminder,
    ReminderKind,
    ReminderState,
)
from app.reminders.schedule import Stage, due_stage, next_stage_after, serialize_schedule

__all__ = [
    "ReminderStatus",
    "reminder_overview",
    "reminder_detail",
    "load_reminder_for_tenant",
]

MAX_ROWS = 500


class ReminderStatus:
    """The owner's five buckets, derived from the schedule and the ledger.

    Deliberately fewer than the states a ``reminder`` row can be in: the row's
    state answers "what happened to that message", while this answers "does this
    customer need me". Those are different questions and the screen asks the
    second one.
    """

    #: A stage is due now and has not been delivered.
    DUE = "DUE"
    #: The due stage went out; the customer still owes, the next stage is later.
    WAITING = "WAITING"
    #: A delivery failed, or the final stage has been reached and money is owed.
    ATTENTION = "ATTENTION"
    #: Nothing is owed, so no further outstanding reminder will be sent (REM-4).
    SETTLED = "SETTLED"
    #: No statement has been issued yet, so there is no cycle to remind against.
    NO_STATEMENT = "NO_STATEMENT"


def _balances(session: Session, ctx) -> dict[uuid.UUID, int]:
    """Every customer's outstanding in one grouped query (FIN-4), not one each."""
    rows = session.execute(
        select(
            LedgerEntry.customer_id,
            func.coalesce(func.sum(LedgerEntry.amount_minor), 0),
        )
        .where(LedgerEntry.tenant_id == ctx.tenant_id)
        .group_by(LedgerEntry.customer_id)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _latest_statements(session: Session, ctx) -> dict[uuid.UUID, tuple[Statement, Any, Any]]:
    """Each customer's most recently issued statement, and its period.

    Mirrors :func:`app.reminders.engine.reminder_cycle_for` — same ordering, same
    definition of "the cycle a reminder chases" — done once for the whole list
    rather than once per customer.
    """
    newest = (
        select(
            Statement.customer_id.label("customer_id"),
            func.max(Statement.row_version).label("row_version"),
        )
        .where(Statement.tenant_id == ctx.tenant_id)
        .group_by(Statement.customer_id)
        .subquery()
    )
    rows = session.execute(
        select(Statement, BillingCycle.period_start, BillingCycle.period_end)
        .join(
            newest,
            (Statement.customer_id == newest.c.customer_id)
            & (Statement.row_version == newest.c.row_version),
        )
        .join(
            BillingCycle,
            (BillingCycle.tenant_id == Statement.tenant_id)
            & (BillingCycle.id == Statement.cycle_id),
        )
        .where(Statement.tenant_id == ctx.tenant_id)
    ).all()
    return {row[0].customer_id: (row[0], row[1], row[2]) for row in rows}


def _reminders_by_customer(
    session: Session, ctx, cycle_ids: dict[uuid.UUID, uuid.UUID]
) -> dict[uuid.UUID, list[Reminder]]:
    if not cycle_ids:
        return {}
    rows = list(
        session.execute(
            select(Reminder)
            .where(
                Reminder.tenant_id == ctx.tenant_id,
                Reminder.customer_id.in_(list(cycle_ids)),
            )
            .order_by(Reminder.schedule_day, Reminder.kind)
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[Reminder]] = {}
    for reminder in rows:
        # Only this customer's *current* reminder cycle; earlier months are
        # history and belong on the customer's own page, not on a work list.
        if cycle_ids.get(reminder.customer_id) != reminder.cycle_id:
            continue
        grouped.setdefault(reminder.customer_id, []).append(reminder)
    return grouped


def _status_for(
    *,
    outstanding: int,
    has_statement: bool,
    stage: Stage | None,
    reminders: list[Reminder],
) -> str:
    if not has_statement:
        return ReminderStatus.NO_STATEMENT
    if outstanding <= 0:
        # REM-4: paid in full, or in credit. Nothing further goes out this cycle.
        return ReminderStatus.SETTLED
    if any(r.state == ReminderState.FAILED for r in reminders):
        return ReminderStatus.ATTENTION
    if stage is None:
        return ReminderStatus.WAITING
    current = [
        r
        for r in reminders
        if r.schedule_day == stage.day and r.kind == stage.kind
    ]
    if not current or current[0].state != ReminderState.SENT:
        return ReminderStatus.DUE
    if stage.kind == ReminderKind.FINAL:
        # The last nudge has been sent and the money is still outstanding. The
        # schedule has nothing left to offer; a person does.
        return ReminderStatus.ATTENTION
    return ReminderStatus.WAITING


def reminder_overview(
    session: Session, ctx, *, limit: int = MAX_ROWS, offset: int = 0
) -> dict[str, Any]:
    """The reminders work list: who owes what, and where each one is in the run.

    Ordered by how much is owed, largest first, which is the order the owner
    actually works in. Customers who owe nothing are included so the screen can
    show that a reminder run stopped for a good reason rather than appearing to
    have missed somebody; the client filters them out by default.
    """
    from app.reminders.engine import tenant_schedule

    limit = max(1, min(limit, MAX_ROWS))
    schedule = tenant_schedule(session, ctx)
    stage = due_stage(schedule, ctx.today.day)

    balances = _balances(session, ctx)
    statements = _latest_statements(session, ctx)
    customers = list(
        session.execute(
            select(Customer)
            .where(Customer.tenant_id == ctx.tenant_id)
            .order_by(Customer.code, Customer.id)
        )
        .scalars()
        .all()
    )
    cycle_ids = {
        c.id: statements[c.id][0].cycle_id for c in customers if c.id in statements
    }
    reminders = _reminders_by_customer(session, ctx, cycle_ids)

    items: list[dict[str, Any]] = []
    for customer in customers:
        outstanding = balances.get(customer.id, 0)
        entry = statements.get(customer.id)
        history = reminders.get(customer.id, [])
        customer_facing = [r for r in history if r.kind != ReminderKind.OWNER_ALERT]
        latest = max(
            (r for r in customer_facing if r.state != ReminderState.PENDING),
            key=lambda r: r.schedule_day,
            default=None,
        )
        status = _status_for(
            outstanding=outstanding,
            has_statement=entry is not None,
            stage=stage,
            reminders=history,
        )
        row: dict[str, Any] = {
            "customer_id": str(customer.id),
            "code": customer.code,
            "name": customer.name,
            "area": customer.area,
            "customer_status": customer.status,
            # FIN-4, live. Never the reminder's stored generation amount.
            "outstanding_minor": outstanding,
            "status": status,
            "has_contact": bool(customer.whatsapp_e164 or customer.phone_e164),
            "cycle": None,
            "latest": serialize_reminder(latest) if latest is not None else None,
            "next_stage": None,
            "owner_alert": None,
            "history": [serialize_reminder(r) for r in history],
        }
        if entry is not None:
            statement, period_start, period_end = entry
            row["cycle"] = {
                "cycle_id": str(statement.cycle_id),
                "statement_id": str(statement.id),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "statement_closing_balance_minor": statement.closing_balance_minor,
            }
        if outstanding > 0 and entry is not None:
            # The next stage is the one after whichever is further along: what
            # has already been sent, or what today has already made due. Reading
            # only the sent stage would announce a day that is already behind us
            # for a customer nothing has been sent to yet — "next: day 1" on the
            # 5th, which is not a thing that can happen.
            reached = max(
                latest.schedule_day if latest is not None else 0,
                stage.day if stage is not None else 0,
            )
            upcoming = next_stage_after(schedule, reached)
            row["next_stage"] = upcoming.as_dict() if upcoming else None
        alert = next((r for r in history if r.kind == ReminderKind.OWNER_ALERT), None)
        if alert is not None:
            row["owner_alert"] = serialize_reminder(alert)
        items.append(row)

    items.sort(key=lambda r: (-r["outstanding_minor"], r["code"]))
    window = items[offset : offset + limit]

    return {
        "business_date": ctx.today.isoformat(),
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
        # REM-1: the schedule is data the screen reads, never numbers it knows.
        "schedule": serialize_schedule(schedule),
        "due_stage": stage.as_dict() if stage else None,
        "counts": {
            "total": len(items),
            "due": sum(1 for r in items if r["status"] == ReminderStatus.DUE),
            "attention": sum(1 for r in items if r["status"] == ReminderStatus.ATTENTION),
            "settled": sum(1 for r in items if r["status"] == ReminderStatus.SETTLED),
        },
        "items": window,
    }


def load_reminder_for_tenant(session: Session, ctx, reminder_id: uuid.UUID) -> Reminder:
    """SEC-3: scoped by the authenticated tenant, so another tenant's id is a 404."""
    reminder = session.execute(
        select(Reminder).where(
            Reminder.tenant_id == ctx.tenant_id, Reminder.id == reminder_id
        )
    ).scalar_one_or_none()
    if reminder is None:
        raise NotFoundError("reminder not found")
    return reminder


def reminder_detail(session: Session, ctx, reminder_id: uuid.UUID) -> dict[str, Any]:
    """One reminder and every delivery attempt made for it.

    The attempt log is what makes a failure investigable: which channel, which
    provider, what it said, and what was actually in the message — already
    rendered, so a reader can confirm no formula or raw balance was ever handed
    to a provider (REM-7).
    """
    reminder = load_reminder_for_tenant(session, ctx, reminder_id)
    attempts = list(
        session.execute(
            select(CommunicationLog)
            .where(
                CommunicationLog.tenant_id == ctx.tenant_id,
                CommunicationLog.reminder_id == reminder.id,
            )
            .order_by(CommunicationLog.attempt_no)
        )
        .scalars()
        .all()
    )
    return {
        **serialize_reminder(reminder),
        "is_outstanding_reminder": reminder.kind in OUTSTANDING_KINDS,
        "outstanding_minor": _current_outstanding(session, ctx, reminder.customer_id),
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
        "attempts": [
            {
                "id": str(row.id),
                "channel": row.channel,
                "provider": row.provider,
                "template_key": row.template_key,
                "state": row.state,
                "error": row.error,
                "attempt_no": row.attempt_no,
                "payload": row.payload,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in attempts
        ],
    }


def _current_outstanding(session: Session, ctx, customer_id: uuid.UUID) -> int:
    from app.billing.ledger import outstanding_minor

    return outstanding_minor(session, ctx, customer_id)
