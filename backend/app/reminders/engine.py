"""The reminder decision engine (P0 §10; REM-1 … REM-8).

Two phases, deliberately separate functions rather than one:

    generate_due_reminder()   decides *whether* a stage exists, and creates it
    dispatch_reminder()       decides *what amount* goes out, and delivers it

They are separate because P0 §10 steps 4 and 5 are separate, and because the gap
between them is where a payment can land. REM-3 is only expressible if a test can
generate a stage for 5000, record a 2000 payment, and then send: the delivered
amount must be 3000. A single fused function could not be asked that question.

**Where the amount comes from.** Always :func:`app.billing.ledger.outstanding_minor`,
recomputed here, at send time, from the append-only ledger (FIN-4, REM-2). Never
from a statement's ``closing_balance_minor``, never from a cache, never from an
earlier reminder's ``amount_minor_at_generation``, and never from anything a
client sent. A statement is what was billed at close; a reminder chases what is
owed *now*, and after a payment those are different numbers.

**Where the cycle comes from.** A reminder chases a customer's most recently
issued statement, and its ``cycle_id`` is that statement's cycle. That is the
"fail safely rather than reminding from fabricated data" rule made concrete: a
customer with no issued statement has no reminder cycle and receives nothing.
It also makes the monthly reset automatic — the next close issues the next
statement, which is a new cycle, whose ``sent_stage`` starts at zero.

**Catch-up.** Entirely :func:`app.reminders.schedule.due_stage`. One stage per
customer per cycle per run, always the latest due one. See REM-8.

**Failure isolation** (REM-6). Nothing in this module imports a payment command,
a statement writer or anything under ``app.commission``. It reads the ledger and
writes ``reminder`` and ``communication_log``. A provider outage therefore
*cannot* move a balance — not because a test says so, but because there is no
code path from here to one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import ActorScope, AuditAction, AuditSource
from app.audit.service import record_audit_event, snapshot
from app.billing.ledger import outstanding_minor
from app.billing.models import BillingCycle, Statement
from app.core.errors import NotFoundError, ValidationFailed
from app.core.money import format_minor
from app.customers.models import Customer
from app.identity.models import Role
from app.ports.comms import (
    Channel,
    CommunicationProvider,
    DeliveryReceipt,
    DeliveryState,
    OutboundMessage,
)
from app.reminders.models import (
    CommunicationLog,
    OUTSTANDING_KINDS,
    Reminder,
    ReminderKind,
    ReminderState,
)
from app.reminders.schedule import Stage, due_stage, load_schedule

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "ReminderCycle",
    "TEMPLATE_FOR_KIND",
    "tenant_schedule",
    "reminder_cycle_for",
    "highest_sent_stage",
    "load_reminder",
    "generate_due_reminder",
    "dispatch_reminder",
    "process_customer",
    "serialize_reminder",
    "serialize_reminders",
    "cycle_reminders",
]

#: How many times one reminder stage may be attempted before it stops retrying.
#:
#: Bounded on purpose (P0 §9: "retry with backoff up to a bounded count and are
#: then surfaced to the owner"). An unbounded retry against a provider that is
#: rejecting the template would burn the whole schedule against one bad message;
#: three attempts, then it sits in the owner's Needs-attention list where a
#: person can look at it.
MAX_DELIVERY_ATTEMPTS = 3

#: The semantic template each stage asks the provider for. Keys only — the body,
#: the language and any vendor template id belong to an adapter (P10).
TEMPLATE_FOR_KIND = {
    ReminderKind.STATEMENT: "statement.issued",
    ReminderKind.REMINDER: "payment.reminder",
    ReminderKind.FINAL: "payment.reminder.final",
    ReminderKind.OWNER_ALERT: "owner.final_alert",
}

_STAGE_UNIQUE_INDEX = "uq_reminder_tenant_id_customer_id_cycle_id_schedule_day_kind"


@dataclass(frozen=True, slots=True)
class ReminderCycle:
    """The billing cycle a customer's reminders currently chase."""

    cycle_id: uuid.UUID
    statement: Statement
    period_start: date
    period_end: date


# --- configuration -----------------------------------------------------------


def tenant_schedule(session: Session, ctx) -> tuple[Stage, ...]:
    """The tenant's own schedule, read from its row (REM-1).

    Not a constant, not a default applied here: if the column is malformed this
    raises, because reminding on days the owner did not configure would be worse
    than not reminding.
    """
    from app.tenancy.models import Tenant

    tenant = session.get(Tenant, ctx.tenant_id)
    if tenant is None:
        raise NotFoundError("tenant not found")
    return load_schedule(tenant.reminder_schedule)


# --- the reminder cycle ------------------------------------------------------


def reminder_cycle_for(session: Session, ctx, customer_id: uuid.UUID) -> ReminderCycle | None:
    """The customer's most recently issued statement, and its cycle.

    ``None`` means there is nothing to remind about yet — no statement has been
    issued for this customer, so there is no bill, no cycle and no stage. That is
    the safe failure: the alternative would be inventing a period and an amount
    out of an open cycle that has not been billed.

    Ordered by ``row_version`` because statements draw it from one monotonic
    sequence at issue, so the newest statement is unambiguous even for two cycles
    closed in the same second.
    """
    row = session.execute(
        select(Statement, BillingCycle.period_start, BillingCycle.period_end)
        .join(
            BillingCycle,
            (BillingCycle.tenant_id == Statement.tenant_id)
            & (BillingCycle.id == Statement.cycle_id),
        )
        .where(Statement.tenant_id == ctx.tenant_id, Statement.customer_id == customer_id)
        .order_by(Statement.row_version.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    statement, period_start, period_end = row
    return ReminderCycle(
        cycle_id=statement.cycle_id,
        statement=statement,
        period_start=period_start,
        period_end=period_end,
    )


def highest_sent_stage(
    session: Session, ctx, customer_id: uuid.UUID, cycle_id: uuid.UUID
) -> int:
    """REM-8's ``sent_stage``: the highest stage *successfully sent* in this cycle.

    ``SENT`` only. A ``FAILED`` stage was not sent, and treating it as sent would
    silently swallow a stage the customer never received; a ``CANCELLED`` one was
    stopped by a payment and is handled by the exact-stage lookup instead. Owner
    alerts are excluded — they are not a customer-facing stage and must not
    advance the customer's progress through the schedule.
    """
    value = session.execute(
        select(func.coalesce(func.max(Reminder.schedule_day), 0)).where(
            Reminder.tenant_id == ctx.tenant_id,
            Reminder.customer_id == customer_id,
            Reminder.cycle_id == cycle_id,
            Reminder.kind != ReminderKind.OWNER_ALERT,
            Reminder.state == ReminderState.SENT,
        )
    ).scalar_one()
    return int(value)


def load_reminder(
    session: Session,
    ctx,
    *,
    customer_id: uuid.UUID,
    cycle_id: uuid.UUID,
    schedule_day: int,
    kind: str,
) -> Reminder | None:
    return session.execute(
        select(Reminder).where(
            Reminder.tenant_id == ctx.tenant_id,
            Reminder.customer_id == customer_id,
            Reminder.cycle_id == cycle_id,
            Reminder.schedule_day == schedule_day,
            Reminder.kind == kind,
        )
    ).scalar_one_or_none()


def cycle_reminders(
    session: Session, ctx, customer_id: uuid.UUID, cycle_id: uuid.UUID
) -> list[Reminder]:
    return list(
        session.execute(
            select(Reminder)
            .where(
                Reminder.tenant_id == ctx.tenant_id,
                Reminder.customer_id == customer_id,
                Reminder.cycle_id == cycle_id,
            )
            .order_by(Reminder.schedule_day, Reminder.kind)
        )
        .scalars()
        .all()
    )


# --- generation --------------------------------------------------------------


def _create_stage(
    session: Session,
    ctx,
    *,
    customer_id: uuid.UUID,
    cycle_id: uuid.UUID,
    stage_day: int,
    kind: str,
    amount_minor: int,
) -> Reminder:
    """Insert one stage row, letting the unique index settle every race.

    REM-5 lives here. Two runners, a retried HTTP call and a doubled cron trigger
    all reach this insert; PostgreSQL picks one winner and the losers reload it.
    There is no pre-read to race with, exactly as ``ensure_open_cycle`` does it.
    """
    reminder = Reminder(
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        cycle_id=cycle_id,
        schedule_day=stage_day,
        kind=kind,
        amount_minor_at_generation=amount_minor,
        state=ReminderState.PENDING,
        generated_at=ctx.now,
    )
    session.add(reminder)
    try:
        session.flush()
    except IntegrityError as exc:
        if _STAGE_UNIQUE_INDEX not in str(getattr(exc, "orig", exc)):
            raise
        session.rollback()
        winner = load_reminder(
            session,
            ctx,
            customer_id=customer_id,
            cycle_id=cycle_id,
            schedule_day=stage_day,
            kind=kind,
        )
        if winner is None:  # pragma: no cover - only if the winner rolled back
            raise
        return winner

    _audit(
        session,
        ctx,
        action=AuditAction.REMINDER_GENERATED,
        reminder=reminder,
        after=snapshot("reminder", reminder),
    )
    return reminder


def generate_due_reminder(
    session: Session,
    ctx,
    *,
    customer: Customer,
    schedule: Sequence[Stage],
    cycle: ReminderCycle | None = None,
) -> list[Reminder]:
    """P0 §10 steps 1-4, for one customer. Returns the stages to dispatch.

    At most **one customer-facing stage** comes back, and it is always the latest
    due one. A ``FINAL`` stage brings its ``OWNER_ALERT`` companion with it, which
    is a second row but not a second stage: the owner is a different recipient,
    not a further nudge to the customer.

    Returns an empty list -- never an exception -- for every ordinary reason not
    to remind: no statement yet, nothing due this early in the month, the stage
    already sent, or the customer no longer owes anything.
    """
    cycle = cycle if cycle is not None else reminder_cycle_for(session, ctx, customer.id)
    if cycle is None:
        # Fail safe: no issued statement means no bill to chase.
        return []

    stage = due_stage(schedule, ctx.today.day)
    if stage is None:
        return []

    due: list[Reminder] = []
    existing = load_reminder(
        session,
        ctx,
        customer_id=customer.id,
        cycle_id=cycle.cycle_id,
        schedule_day=stage.day,
        kind=stage.kind,
    )

    if existing is None:
        # REM-8: only the latest due stage, and never a replay of a skipped one.
        if stage.day <= highest_sent_stage(session, ctx, customer.id, cycle.cycle_id):
            return []
        outstanding = outstanding_minor(session, ctx, customer.id)
        if stage.kind in OUTSTANDING_KINDS and outstanding <= 0:
            # REM-4. A customer who has paid in full -- or is in credit -- gets no
            # outstanding reminder, and no stage row is created for one either.
            return []
        existing = _create_stage(
            session,
            ctx,
            customer_id=customer.id,
            cycle_id=cycle.cycle_id,
            stage_day=stage.day,
            kind=stage.kind,
            amount_minor=outstanding,
        )
        due.append(existing)
    elif _is_retryable(existing):
        # A stage that exists but has not been delivered: still the due stage, so
        # this run may attempt it again. A CANCELLED one is never resurrected --
        # a payment stopped it, and a later stage is where the schedule picks the
        # customer up again.
        due.append(existing)

    if stage.kind == ReminderKind.FINAL and existing.state != ReminderState.CANCELLED:
        alert = _owner_alert_for(session, ctx, final=existing, customer=customer, cycle=cycle)
        if alert is not None:
            due.append(alert)
    return due


def _is_retryable(reminder: Reminder) -> bool:
    """Whether an existing stage row may be attempted again in this run."""
    if reminder.state == ReminderState.PENDING:
        return True
    return (
        reminder.state == ReminderState.FAILED
        and reminder.attempt_count < MAX_DELIVERY_ATTEMPTS
    )


def _owner_alert_for(
    session: Session, ctx, *, final: Reminder, customer: Customer, cycle: ReminderCycle
) -> Reminder | None:
    """The owner alert P0 §10 puts beside the ``FINAL`` stage.

    Derived from the final stage rather than configured as a schedule entry of
    its own, so it cannot drift away from it, and it inherits the final stage's
    eligibility -- a customer who paid gets neither. It is a separate row so the
    same unique index that makes a reminder exactly-once makes the alert
    exactly-once, and it is deliberately excluded from ``sent_stage`` so alerting
    the owner never advances the *customer* through the schedule.

    A ``FINAL`` that already succeeded still reaches here, which is what lets a
    failed alert be retried on a later run without re-sending the customer's
    final notice.
    """
    alert = load_reminder(
        session,
        ctx,
        customer_id=customer.id,
        cycle_id=cycle.cycle_id,
        schedule_day=final.schedule_day,
        kind=ReminderKind.OWNER_ALERT,
    )
    if alert is None:
        return _create_stage(
            session,
            ctx,
            customer_id=customer.id,
            cycle_id=cycle.cycle_id,
            stage_day=final.schedule_day,
            kind=ReminderKind.OWNER_ALERT,
            amount_minor=outstanding_minor(session, ctx, customer.id),
        )
    return alert if _is_retryable(alert) else None


# --- destinations ------------------------------------------------------------


def _customer_destination(customer: Customer) -> tuple[str, str] | None:
    """Where a customer reminder goes: ``(channel, destination)``.

    WhatsApp is preferred where the customer has one, because that is the channel
    the business actually uses; the ordinary phone number is the fallback and
    reaches the same port under a different :class:`Channel`. A customer with
    neither has no destination, which is a *delivery* failure the owner can see
    and fix, not a silent skip and not an invented address.
    """
    if customer.whatsapp_e164:
        return Channel.WHATSAPP, customer.whatsapp_e164
    if customer.phone_e164:
        return Channel.SMS, customer.phone_e164
    return None


def _owner_destination(session: Session, ctx) -> tuple[str, str] | None:
    """Where the day-15 owner alert goes.

    The tenant's own owner-admin, by the email address on their identity — the
    only owner contact the data model holds (P0 §6 gives ``app_user`` no phone).
    Each person keeps their own account, so the alert reaches a named human and
    stays attributable rather than going to a shared inbox.
    """
    from app.identity.models import AppUser

    email = session.execute(
        select(AppUser.email)
        .where(
            AppUser.tenant_id == ctx.tenant_id,
            AppUser.role == Role.OWNER_ADMIN,
            AppUser.status == "ACTIVE",
        )
        .order_by(AppUser.created_at, AppUser.id)
        .limit(1)
    ).scalar_one_or_none()
    if not email:
        return None
    return Channel.EMAIL, email


# --- rendering ---------------------------------------------------------------


def _render_params(
    ctx,
    *,
    customer: Customer,
    cycle: ReminderCycle,
    reminder: Reminder,
    outstanding_now: int,
) -> dict[str, str]:
    """The message body's values — every one an already-rendered string (REM-7).

    The provider receives "PKR 3,000.00", never ``300000`` and never a currency
    exponent to apply. There is no instruction, no formula and no raw balance
    here for anything downstream to interpret; :class:`OutboundMessage` rejects
    a non-string value and any key ending in ``_minor`` so this cannot regress.
    """
    amount = format_minor(outstanding_now, ctx.currency, ctx.currency_exponent)
    params = {
        "customer_name": customer.name,
        "customer_code": customer.code,
        "amount_due": amount,
        "currency": ctx.currency,
        "period_start": cycle.period_start.isoformat(),
        "period_end": cycle.period_end.isoformat(),
        "business_date": ctx.today.isoformat(),
        "stage": str(reminder.schedule_day),
    }
    if reminder.kind == ReminderKind.STATEMENT:
        params["statement_total"] = format_minor(
            cycle.statement.closing_balance_minor,
            cycle.statement.currency,
            ctx.currency_exponent,
        )
    return params


# --- dispatch ----------------------------------------------------------------


def dispatch_reminder(
    session: Session,
    ctx,
    reminder: Reminder,
    provider: CommunicationProvider,
    *,
    customer: Customer | None = None,
    cycle: ReminderCycle | None = None,
    operation_id: uuid.UUID | None = None,
    manual: bool = False,
) -> Reminder:
    """P0 §10 step 5: re-check eligibility, re-read the amount, then deliver.

    The amount that goes out is read **here**, not at generation. A payment that
    landed in between lowers it (REM-3); a payment that cleared the balance
    cancels the stage outright (REM-4) and nothing is sent.

    Every outcome is durable. An accepted delivery marks the stage ``SENT``; a
    refusal, an exception or a missing contact marks it ``FAILED`` with the
    reason and appends a ``communication_log`` row either way. A failure is never
    quietly upgraded to a success and never silently dropped — B and C of the
    delivery-failure contract, and the only two states a retry can start from.
    """
    if reminder.state in (ReminderState.SENT, ReminderState.CANCELLED):
        return reminder
    if reminder.attempt_count >= MAX_DELIVERY_ATTEMPTS:
        # Bounded (P0 §9). It stays FAILED and stays visible; it does not loop.
        return reminder

    if customer is None:
        customer = session.execute(
            select(Customer).where(
                Customer.tenant_id == ctx.tenant_id, Customer.id == reminder.customer_id
            )
        ).scalar_one_or_none()
        if customer is None:
            raise NotFoundError("customer not found")
    if cycle is None:
        cycle = reminder_cycle_for(session, ctx, reminder.customer_id)
        if cycle is None or cycle.cycle_id != reminder.cycle_id:
            cycle = _cycle_by_id(session, ctx, reminder)

    # REM-2: the authoritative outstanding, now, from the ledger.
    outstanding_now = outstanding_minor(session, ctx, reminder.customer_id)
    if reminder.kind in OUTSTANDING_KINDS and outstanding_now <= 0:
        return _cancel(session, ctx, reminder, reason="customer no longer owes anything")

    if reminder.kind == ReminderKind.OWNER_ALERT:
        destination = _owner_destination(session, ctx)
    else:
        destination = _customer_destination(customer)
    if destination is None:
        return _fail(
            session,
            ctx,
            reminder,
            error=(
                "no active owner-admin address to alert"
                if reminder.kind == ReminderKind.OWNER_ALERT
                else "no phone or WhatsApp number on file for this customer"
            ),
            manual=manual,
            operation_id=operation_id,
        )

    channel, to = destination
    params = _render_params(
        ctx,
        customer=customer,
        cycle=cycle,
        reminder=reminder,
        outstanding_now=outstanding_now,
    )

    attempt_no = reminder.attempt_count + 1
    entry = CommunicationLog(
        tenant_id=ctx.tenant_id,
        customer_id=reminder.customer_id,
        reminder_id=reminder.id,
        channel=channel,
        provider=getattr(provider, "name", "unknown"),
        template_key=TEMPLATE_FOR_KIND[reminder.kind],
        destination=to,
        payload=params,
        state=DeliveryState.QUEUED,
        attempt_no=attempt_no,
    )
    session.add(entry)
    session.flush()

    message = OutboundMessage(
        tenant_id=ctx.tenant_id,
        customer_id=reminder.customer_id,
        channel=channel,
        to=to,
        template_key=entry.template_key,
        params=params,
        # P0 §9: the reminder's own id, so a retry of an uncertain delivery is
        # visibly the same logical delivery to any provider that deduplicates.
        idempotency_key=reminder.id,
        reference={"cycle_id": str(reminder.cycle_id), "stage": str(reminder.schedule_day)},
    )

    try:
        receipt = provider.send(message)
    except Exception as exc:  # a provider outage is a delivery fact, not a crash
        receipt = DeliveryReceipt(
            state=DeliveryState.FAILED,
            provider=getattr(provider, "name", "unknown"),
            error=f"{type(exc).__name__}: {exc}",
        )

    entry.state = receipt.state
    entry.provider_message_id = receipt.provider_message_id
    entry.error = receipt.error
    reminder.attempt_count = attempt_no

    if receipt.succeeded:
        reminder.state = ReminderState.SENT
        reminder.sent_at = ctx.now
        reminder.last_error = None
        _audit(
            session,
            ctx,
            action=(
                AuditAction.REMINDER_OWNER_ALERTED
                if reminder.kind == ReminderKind.OWNER_ALERT
                else AuditAction.REMINDER_SENT
            ),
            reminder=reminder,
            after=snapshot("reminder", reminder),
            reason="re-dispatched by the owner" if manual else None,
            operation_id=operation_id,
            manual=manual,
        )
    else:
        reminder.state = ReminderState.FAILED
        reminder.last_error = receipt.error or "delivery failed"
        _audit(
            session,
            ctx,
            action=AuditAction.REMINDER_FAILED,
            reminder=reminder,
            after=snapshot("reminder", reminder),
            reason=reminder.last_error,
            operation_id=operation_id,
            manual=manual,
        )

    session.flush()
    return reminder


def _cycle_by_id(session: Session, ctx, reminder: Reminder) -> ReminderCycle:
    row = session.execute(
        select(Statement, BillingCycle.period_start, BillingCycle.period_end)
        .join(
            BillingCycle,
            (BillingCycle.tenant_id == Statement.tenant_id)
            & (BillingCycle.id == Statement.cycle_id),
        )
        .where(
            Statement.tenant_id == ctx.tenant_id,
            Statement.customer_id == reminder.customer_id,
            Statement.cycle_id == reminder.cycle_id,
        )
        .limit(1)
    ).one_or_none()
    if row is None:
        raise ValidationFailed(
            "the reminder's billing cycle no longer has an issued statement"
        )
    statement, period_start, period_end = row
    return ReminderCycle(
        cycle_id=statement.cycle_id,
        statement=statement,
        period_start=period_start,
        period_end=period_end,
    )


def _cancel(session: Session, ctx, reminder: Reminder, *, reason: str) -> Reminder:
    reminder.state = ReminderState.CANCELLED
    reminder.cancelled_at = ctx.now
    reminder.last_error = None
    _audit(
        session,
        ctx,
        action=AuditAction.REMINDER_CANCELLED,
        reminder=reminder,
        after=snapshot("reminder", reminder),
        reason=reason,
    )
    session.flush()
    return reminder


def _fail(
    session: Session,
    ctx,
    reminder: Reminder,
    *,
    error: str,
    manual: bool = False,
    operation_id: uuid.UUID | None = None,
) -> Reminder:
    """Record a failure that never reached the provider.

    Today that means exactly one thing: there is no contact on file to send to.
    No ``communication_log`` row is written, because nothing was handed to a
    provider and a log row would claim an attempt that never happened -- the
    reminder's own ``FAILED`` state, its ``last_error`` and the audit row are the
    durable record. It is still counted as an attempt and still surfaces in the
    owner's list: skipping the customer silently would hide a data problem only
    the owner can fix.
    """
    reminder.attempt_count += 1
    reminder.state = ReminderState.FAILED
    reminder.last_error = error
    _audit(
        session,
        ctx,
        action=AuditAction.REMINDER_FAILED,
        reminder=reminder,
        after=snapshot("reminder", reminder),
        reason=error,
        operation_id=operation_id,
        manual=manual,
    )
    session.flush()
    return reminder


def _audit(
    session: Session,
    ctx,
    *,
    action: str,
    reminder: Reminder,
    after: dict[str, Any] | None,
    before: dict[str, Any] | None = None,
    reason: str | None = None,
    operation_id: uuid.UUID | None = None,
    manual: bool = False,
) -> None:
    """AUD-9: the runner is ``SYSTEM``/``JOB``; an owner's re-dispatch is not.

    Provenance is what makes reminder history readable a month later — "the cron
    sent this" and "a person re-sent this" must never look the same.
    """
    system = getattr(ctx, "user_id", None) is None and not manual
    record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_user_id=None if system else getattr(ctx, "user_id", None),
        actor_scope=ActorScope.SYSTEM if system else ActorScope.TENANT,
        action=action,
        entity_type="reminder",
        entity_id=reminder.id,
        before=before,
        after=after,
        reason=reason,
        operation_id=operation_id,
        source=AuditSource.ONLINE if manual else AuditSource.JOB,
    )


# --- one customer, end to end ------------------------------------------------


def process_customer(
    session: Session,
    ctx,
    provider: CommunicationProvider,
    *,
    customer: Customer,
    schedule: Sequence[Stage],
) -> list[Reminder]:
    """Generate the due stage for one customer and deliver it.

    The runner's unit of work, and its unit of commit: the caller commits after
    each customer, so a crash halfway through a round leaves the customers
    already handled durably handled and the rest untouched. A retry then picks up
    where it stopped rather than starting again — and could not double-send even
    if it did, because the stage row already exists.
    """
    cycle = reminder_cycle_for(session, ctx, customer.id)
    if cycle is None:
        return []
    reminders = generate_due_reminder(
        session, ctx, customer=customer, schedule=schedule, cycle=cycle
    )
    for reminder in reminders:
        dispatch_reminder(
            session, ctx, reminder, provider, customer=customer, cycle=cycle
        )
    return reminders


# --- serialization -----------------------------------------------------------


def serialize_reminder(reminder: Reminder) -> dict[str, Any]:
    return {
        "id": str(reminder.id),
        "customer_id": str(reminder.customer_id),
        "cycle_id": str(reminder.cycle_id),
        "schedule_day": reminder.schedule_day,
        "kind": reminder.kind,
        "state": reminder.state,
        "amount_minor_at_generation": reminder.amount_minor_at_generation,
        "attempt_count": reminder.attempt_count,
        "last_error": reminder.last_error,
        "generated_at": reminder.generated_at.isoformat() if reminder.generated_at else None,
        "sent_at": reminder.sent_at.isoformat() if reminder.sent_at else None,
        "cancelled_at": reminder.cancelled_at.isoformat() if reminder.cancelled_at else None,
    }


def serialize_reminders(reminders: Iterable[Reminder]) -> list[dict[str, Any]]:
    return [serialize_reminder(r) for r in reminders]
