"""P7 reminder engine: the stage register, the delivery log, the job guard.

Revision ID: 0005_p7_reminder_engine
Revises: 0004_p6_operating_costs
Create Date: P7 — Reminder Engine

Adds exactly the three tables P0 §6 named and earlier packages deliberately left
absent: ``reminder``, ``communication_log`` and ``job_run``. With these, every
table the architecture freeze specified exists; a new table after this one is a
new decision rather than a deferred one.

**Nothing else in the schema moves.** No column is added to ``ledger_entry``,
``payment``, ``statement`` or any ``commission_*`` or ``operating_cost_*`` table.
A reminder reads a balance and never writes one, which is REM-6 expressed as an
absence of foreign keys rather than as a promise.

**The stage index is the whole of REM-5.** ``(tenant_id, customer_id, cycle_id,
schedule_day, kind)`` unique is what makes a doubled cron trigger, an overlapping
runner and a retried HTTP call all produce one message instead of three. P0 §6
freezes the key without ``kind``; P0 §10 puts two communications on day 15 — the
customer's FINAL and the owner's OWNER_ALERT — which that key cannot express, so
``kind`` joins it. REM-5's guarantee is unchanged, because the schedule maps each
day to exactly one customer-facing kind.

**``job_run`` is a short-circuit, not the guarantee.** ``(tenant_id, kind,
business_date)`` unique makes a same-day re-run a no-op (A-REM-5). Correctness
under genuine concurrency comes from the stage index above, which holds even if
two runners proceed at once.

**No ``row_version`` anywhere here.** None of these is a client sync entity:
reminder generation and delivery are server-only and no reminder write enters the
P5 outbox. A version column would quietly make them syncable and invite a later
feed reader to stream them.

**Deletes are blocked by trigger; updates are not.** A reminder has a lifecycle
(``PENDING -> SENT / FAILED / CANCELLED``) and a delivery row has one too
(``QUEUED -> ACCEPTED -> DELIVERED``), both named by P0 §6, so blocking UPDATE
would forbid the transitions the freeze describes. What is blocked is DELETE:
"we reminded them and they still did not pay" is evidence (AUD-1).

**No ``btree_gist``, no EXCLUDE.** Unlike commission plans and cost rates, a
reminder stage is a point rather than a range; a plain unique index is the right
instrument and a heavier one would suggest a guarantee that is not being made.

Written by hand for the same reason as every earlier migration: every constraint
has an explicit, stable name so the schema-assertion test can look it up rather
than trusting this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_p7_reminder_engine"
down_revision = "0004_p6_operating_costs"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)

_KIND = "('STATEMENT','REMINDER','FINAL','OWNER_ALERT')"
_STATE = "('PENDING','SENT','FAILED','CANCELLED')"
_DELIVERY_STATE = "('QUEUED','ACCEPTED','DELIVERED','FAILED')"
_CHANNEL = "('WHATSAPP','SMS','EMAIL')"
_JOB_STATUS = "('RUNNING','SUCCEEDED','FAILED')"
_TRIGGER = "('CRON','MANUAL')"


def upgrade() -> None:
    # ------------------------------------------------------------- reminder
    op.create_table(
        "reminder",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("customer_id", UUID, nullable=False),
        sa.Column("cycle_id", UUID, nullable=False),
        sa.Column("schedule_day", sa.SmallInteger, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        # The balance when the stage was generated. Never the amount delivered:
        # REM-2/REM-3 re-read the authoritative outstanding at send time.
        sa.Column("amount_minor_at_generation", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("generated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", TS, nullable=True),
        sa.Column("cancelled_at", TS, nullable=True),
        # Composite target for communication_log's (tenant_id, reminder_id).
        sa.UniqueConstraint("tenant_id", "id", name="uq_reminder_tenant_id_id"),
        # REM-5, in the database rather than in a code path.
        sa.UniqueConstraint(
            "tenant_id",
            "customer_id",
            "cycle_id",
            "schedule_day",
            "kind",
            name="uq_reminder_tenant_id_customer_id_cycle_id_schedule_day_kind",
        ),
        # SEC-2: a reminder can only name its own tenant's customer and cycle.
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_reminder_tenant_id_customer_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["billing_cycle.tenant_id", "billing_cycle.id"],
            name="fk_reminder_tenant_id_cycle_id",
        ),
        sa.CheckConstraint(f"kind IN {_KIND}", name="kind_valid"),
        sa.CheckConstraint(f"state IN {_STATE}", name="state_valid"),
        # 1..28 for the same reason tenant.cycle_start_day is bounded: a stage on
        # the 31st does not occur in February.
        sa.CheckConstraint("schedule_day BETWEEN 1 AND 28", name="schedule_day_range"),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        sa.CheckConstraint(
            "(state = 'SENT') = (sent_at IS NOT NULL)", name="sent_at_matches_state"
        ),
        sa.CheckConstraint(
            "(state = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="cancelled_at_matches_state",
        ),
    )
    op.create_index(
        "ix_reminder_tenant_id_customer_id_cycle_id",
        "reminder",
        ["tenant_id", "customer_id", "cycle_id"],
    )
    op.create_index("ix_reminder_tenant_id_state", "reminder", ["tenant_id", "state"])

    # ---------------------------------------------------- communication_log
    op.create_table(
        "communication_log",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("customer_id", UUID, nullable=True),
        sa.Column("reminder_id", UUID, nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("template_key", sa.String(64), nullable=False),
        sa.Column("destination", sa.String(320), nullable=False),
        # The already-rendered values handed to the provider. Strings only, which
        # is what makes A-REM-7 checkable after the fact.
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("attempt_no", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "reminder_id"],
            ["reminder.tenant_id", "reminder.id"],
            name="fk_communication_log_tenant_id_reminder_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_communication_log_tenant_id_customer_id",
        ),
        sa.CheckConstraint(f"state IN {_DELIVERY_STATE}", name="state_valid"),
        sa.CheckConstraint(f"channel IN {_CHANNEL}", name="channel_valid"),
        sa.CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
    )
    op.create_index(
        "ix_communication_log_tenant_id_reminder_id",
        "communication_log",
        ["tenant_id", "reminder_id"],
    )
    op.create_index(
        "ix_communication_log_tenant_id_created_at",
        "communication_log",
        ["tenant_id", "created_at"],
    )

    # -------------------------------------------------------------- job_run
    op.create_table(
        "job_run",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        # The *tenant's* business date, resolved server-side from its timezone
        # (P0 R4) — never the host's date and never one a caller supplied.
        sa.Column("business_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="RUNNING"),
        sa.Column("triggered_by", sa.String(16), nullable=False, server_default="CRON"),
        sa.Column("detail", postgresql.JSONB, nullable=True),
        sa.Column("started_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", TS, nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "business_date",
            name="uq_job_run_tenant_id_kind_business_date",
        ),
        sa.CheckConstraint(f"status IN {_JOB_STATUS}", name="status_valid"),
        sa.CheckConstraint(f"triggered_by IN {_TRIGGER}", name="triggered_by_valid"),
    )

    # AUD-1 / AUD-7: reminder history has **no hard-delete path**.
    #
    # DELETE only, not UPDATE. Both tables carry a lifecycle P0 §6 specifies —
    # a reminder moves PENDING -> SENT / FAILED / CANCELLED, and a delivery row
    # moves QUEUED -> ACCEPTED -> DELIVERED as a provider reports back — so
    # blocking UPDATE would forbid the very transitions the freeze describes.
    # What must never happen is a row disappearing: "we reminded them and they
    # still did not pay" is evidence, and evidence is not deleted.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reminder_history_no_delete()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'reminder history has no delete path (AUD-1): % on % is not permitted',
                TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("reminder", "communication_log"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reminder_history_no_delete()
            """
        )


def downgrade() -> None:
    for table in ("communication_log", "reminder"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table}")
    op.drop_table("job_run")
    op.execute("DROP TABLE communication_log")
    op.execute("DROP TABLE reminder")
    op.execute("DROP FUNCTION IF EXISTS reminder_history_no_delete()")
