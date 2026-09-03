"""Reminder tables: the stage register, the delivery log, and the job guard.

Three tables, exactly as P0 §6 names them, and nothing beyond them.

**``reminder`` is the stage register, not a message.** One row means "this
customer's day-N stage for this billing cycle exists". Its uniqueness is what
makes REM-5 true in the database rather than in a code path: a duplicated cron
trigger, a retried HTTP call and two concurrent runners all collide on the same
index and exactly one row survives.

    Frozen key, and where P7 widened it. P0 §6 freezes
    ``(tenant_id, customer_id, cycle_id, schedule_day)``, while P0 §10 puts *two*
    communications on day 15 — the customer's ``FINAL`` and the owner's
    ``OWNER_ALERT``. The frozen key cannot express both. ``kind`` therefore joins
    the key. REM-5's guarantee is untouched: the schedule maps each day to
    exactly one customer-facing kind, so there is still at most one reminder per
    customer per stage, and the owner alert now gets the same exactly-once
    guarantee from the same index instead of from application care.

**``communication_log`` is the attempt history**: one row per delivery attempt,
carrying what was actually sent and what the provider said. Nothing in billing
reads it (P0 §6), and a failure here can never move a balance, a statement, a
payment or a commission row (REM-6). Its ``state`` advances as a provider reports
back, so it is not append-only in the ``ledger_entry`` sense — but it has **no
delete path**, blocked by trigger, because reminder history is evidence (AUD-1).

**``job_run`` is the same-day guard.** ``(tenant_id, kind, business_date)`` is
unique, so a cron that fires twice on one business date runs once. It is a
short-circuit, not the correctness guarantee — that is the ``reminder`` index
above, which holds even if two runners somehow proceed at once.

**No ``row_version`` on any of them.** None is a client sync entity: reminder
generation and delivery are server-only and no reminder write ever enters the P5
outbox. A version column would quietly make them syncable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, business_day, uuid_fk, uuid_nullable, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = [
    "Reminder",
    "ReminderKind",
    "ReminderState",
    "CommunicationLog",
    "JobRun",
    "JobKind",
    "JobRunStatus",
    "JobTrigger",
    "OUTSTANDING_KINDS",
    "CUSTOMER_KINDS",
]


class ReminderKind:
    """P0 §10. ``OWNER_ALERT`` is not a schedule entry — it accompanies ``FINAL``."""

    STATEMENT = "STATEMENT"
    REMINDER = "REMINDER"
    FINAL = "FINAL"
    OWNER_ALERT = "OWNER_ALERT"

    ALL = frozenset({STATEMENT, REMINDER, FINAL, OWNER_ALERT})


#: Kinds whose eligibility rule is "outstanding > 0" (REM-4).
#:
#: ``STATEMENT`` is deliberately absent: a statement is a bill and a record, not
#: a dunning notice, so it goes to every active customer with an issued statement
#: including one who owes nothing (P0 §10 step 3).
OUTSTANDING_KINDS: frozenset[str] = frozenset(
    {ReminderKind.REMINDER, ReminderKind.FINAL, ReminderKind.OWNER_ALERT}
)

#: Kinds addressed to the customer. The owner alert is not one of them.
CUSTOMER_KINDS: frozenset[str] = frozenset(
    {ReminderKind.STATEMENT, ReminderKind.REMINDER, ReminderKind.FINAL}
)


class ReminderState:
    """P0 §6.

    ``CANCELLED`` is what a mid-cycle payment produces: the stage was generated,
    the balance then went to zero, and REM-4 says nothing further goes out. It is
    recorded rather than deleted, so the owner can see that a reminder was
    stopped and why.
    """

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    ALL = frozenset({PENDING, SENT, FAILED, CANCELLED})


class Reminder(Base):
    __tablename__ = "reminder"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]
    cycle_id: Mapped[uuid_fk]

    schedule_day: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # The balance when the stage was generated. **Not** the amount delivered:
    # REM-2/REM-3 re-read the authoritative outstanding at send time, and what
    # actually went out is the rendered string in ``communication_log.payload``.
    # Kept because the difference between the two is exactly what a person
    # investigating "why did they get that number" needs to see.
    amount_minor_at_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    generated_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_reminder_tenant_id_id"),
        # REM-5, in the database. See the module docstring for why ``kind`` is here.
        UniqueConstraint(
            "tenant_id",
            "customer_id",
            "cycle_id",
            "schedule_day",
            "kind",
            name="uq_reminder_tenant_id_customer_id_cycle_id_schedule_day_kind",
        ),
        # SEC-2: a reminder can only ever name its own tenant's customer and cycle.
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_reminder_tenant_id_customer_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["billing_cycle.tenant_id", "billing_cycle.id"],
            name="fk_reminder_tenant_id_cycle_id",
        ),
        Index(
            "ix_reminder_tenant_id_customer_id_cycle_id",
            "tenant_id",
            "customer_id",
            "cycle_id",
        ),
        Index("ix_reminder_tenant_id_state", "tenant_id", "state"),
        CheckConstraint(
            "kind IN ('STATEMENT','REMINDER','FINAL','OWNER_ALERT')", name="kind_valid"
        ),
        CheckConstraint(
            "state IN ('PENDING','SENT','FAILED','CANCELLED')", name="state_valid"
        ),
        # 1..28 for the same reason ``tenant.cycle_start_day`` is bounded: a stage
        # on the 31st has no meaning in February.
        CheckConstraint("schedule_day BETWEEN 1 AND 28", name="schedule_day_range"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        # A SENT reminder always carries its instant, and only a SENT one does.
        CheckConstraint(
            "(state = 'SENT') = (sent_at IS NOT NULL)", name="sent_at_matches_state"
        ),
        CheckConstraint(
            "(state = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="cancelled_at_matches_state",
        ),
    )


class CommunicationLog(Base):
    """One delivery attempt, and what the provider said about it.

    ``payload`` holds the rendered values that were handed to the provider —
    strings, already formatted — which is what makes A-REM-7 checkable after the
    fact: a reader can see there was no formula and no raw balance in the message.
    """

    __tablename__ = "communication_log"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_nullable]
    reminder_id: Mapped[uuid_nullable]

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    template_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Never more than the tenant already stores: this is the same phone number or
    # email address the customer or owner-admin row holds.
    destination: Mapped[str] = mapped_column(String(320), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "reminder_id"],
            ["reminder.tenant_id", "reminder.id"],
            name="fk_communication_log_tenant_id_reminder_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_communication_log_tenant_id_customer_id",
        ),
        Index("ix_communication_log_tenant_id_reminder_id", "tenant_id", "reminder_id"),
        Index("ix_communication_log_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint(
            "state IN ('QUEUED','ACCEPTED','DELIVERED','FAILED')", name="state_valid"
        ),
        CheckConstraint("channel IN ('WHATSAPP','SMS','EMAIL')", name="channel_valid"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
    )


class JobKind:
    REMINDERS = "reminders"


class JobRunStatus:
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    ALL = frozenset({RUNNING, SUCCEEDED, FAILED})


class JobTrigger:
    CRON = "CRON"
    MANUAL = "MANUAL"


class JobRun(Base):
    """One execution of a scheduled job for one tenant on one business date.

    The business date is the *tenant's*, resolved server-side from its timezone
    (P0 R4) — never the host's date and never a date a caller supplied. A cron in
    one timezone driving tenants in another therefore still guards correctly.
    """

    __tablename__ = "job_run"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    business_date: Mapped[business_day]

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RUNNING")
    triggered_by: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="CRON"
    )
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "kind",
            "business_date",
            name="uq_job_run_tenant_id_kind_business_date",
        ),
        CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED')", name="status_valid"),
        CheckConstraint("triggered_by IN ('CRON','MANUAL')", name="triggered_by_valid"),
    )
