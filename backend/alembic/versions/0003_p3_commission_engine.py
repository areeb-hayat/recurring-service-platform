"""P3 commercial tracking: commission plans, events, adjustments, settlements.

Revision ID: 0003_p3_commission_engine
Revises: 0002_p2_financial_engine
Create Date: P3 — Commercial Tracking / Commission

Adds exactly the four P0 §6 commission tables and nothing else. Reminder,
communication_log and job_run remain absent — they belong to later packages —
and there is deliberately **no settlement-allocation table** and **no
``settlement_id`` column** on any earning row (COM-11, P0 §11.1).

None of these tables carries ``row_version``. P0 §6 lists the ``commission_*``
family among the server-side tables that are not client sync entities; commission
never appears in a tenant's offline snapshot, and versioning it "for symmetry"
would quietly make it one.

Written by hand for the same reason as the earlier migrations: every constraint
named in P0 §6 is explicit here with a stable name, so the schema-assertion test
can look it up rather than trusting this file.

**``btree_gist``.** P0 §6 requires that commission plan effective ranges "must not
overlap per tenant". Enforcing that in the database needs an EXCLUDE constraint
combining an equality test on ``tenant_id`` with an overlap test on a
``daterange``, and GiST only indexes UUID equality through ``btree_gist``. It is a
standard PostgreSQL contrib module, present on every managed provider P0 §14
contemplates, and it is what forced the dependency: the alternative is a
read-then-write check in application code, which races and which P0 §3.4's
"not merely unlikely" standard rules out for exactly this kind of guarantee.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_p3_commission_engine"
down_revision = "0002_p2_financial_engine"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)

_BASIS = "('RECORDED_VALUE','BILLED_VALUE','COLLECTED_VALUE','PER_EVENT')"
_SOURCE_TYPE = "('daily_service_record','payment','statement')"

# COM-3 / COM-6 / AUD-1 made structural, the same way P2 protected `statement`.
# Resting immutability on the absence of a route is true today and one careless
# UPDATE away from false; these three tables are earned history and settled money.
_IMMUTABLE_TABLES = (
    "commission_event",
    "commission_adjustment",
    "commission_settlement",
)


def upgrade() -> None:
    # Required by the plan exclusion constraint below; see the module docstring.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # -------------------------------------------------------- commission_plan
    op.create_table(
        "commission_plan",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("basis", sa.String(24), nullable=False),
        sa.Column("rate_bp", sa.Integer, nullable=True),
        sa.Column("fixed_amount_minor", sa.BigInteger, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date, nullable=True),
        sa.Column(
            "created_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        # Composite target for (tenant_id, plan_id) on commission_event.
        sa.UniqueConstraint("tenant_id", "id", name="uq_commission_plan_tenant_id_id"),
        sa.CheckConstraint(f"basis IN {_BASIS}", name="basis_valid"),
        # COM-9: an integer 0..10000 basis points, enforced by the database.
        sa.CheckConstraint(
            "rate_bp IS NULL OR rate_bp BETWEEN 0 AND 10000", name="rate_bp_range"
        ),
        sa.CheckConstraint(
            "fixed_amount_minor IS NULL OR fixed_amount_minor >= 0",
            name="fixed_amount_non_negative",
        ),
        # P0 §6: exactly one of rate_bp / fixed_amount_minor is set — and it is
        # the one the basis actually uses, so a PER_EVENT plan cannot carry a rate.
        sa.CheckConstraint(
            "(basis = 'PER_EVENT' AND fixed_amount_minor IS NOT NULL AND rate_bp IS NULL)"
            " OR (basis <> 'PER_EVENT' AND rate_bp IS NOT NULL "
            "AND fixed_amount_minor IS NULL)",
            name="exactly_one_term_for_basis",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
    )
    op.create_index(
        "ix_commission_plan_tenant_id_effective_from",
        "commission_plan",
        ["tenant_id", "effective_from"],
    )
    # P0 §6: "Effective ranges must not overlap per tenant." A NULL effective_to
    # is an unbounded upper bound, so an open-ended plan excludes every later one
    # until it is closed. This *is* the guarantee — application code never
    # pre-reads to decide.
    op.execute(
        """
        ALTER TABLE commission_plan
        ADD CONSTRAINT ex_commission_plan_effective_range_no_overlap
        EXCLUDE USING gist (
            tenant_id WITH =,
            daterange(effective_from, effective_to, '[]') WITH &&
        )
        """
    )

    # ------------------------------------------------------- commission_event
    op.create_table(
        "commission_event",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("basis_snapshot", sa.String(24), nullable=False),
        sa.Column("rate_bp_snapshot", sa.Integer, nullable=True),
        sa.Column("fixed_amount_minor_snapshot", sa.BigInteger, nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger, nullable=False),
        sa.Column("commission_minor", sa.BigInteger, nullable=False),
        sa.Column("occurred_on", sa.Date, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_commission_event_tenant_id_id"),
        # SEC-2: composite, so an event cannot cite another tenant's plan.
        sa.ForeignKeyConstraint(
            ["tenant_id", "plan_id"],
            ["commission_plan.tenant_id", "commission_plan.id"],
            name="fk_commission_event_tenant_id_plan_id",
        ),
        # COM-5: one source fact yields at most one earning event. This index is
        # what makes a replayed or duplicated source incapable of earning twice.
        sa.UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            name="uq_commission_event_tenant_id_source_type_source_id",
        ),
        sa.CheckConstraint(f"basis_snapshot IN {_BASIS}", name="basis_snapshot_valid"),
        sa.CheckConstraint(f"source_type IN {_SOURCE_TYPE}", name="source_type_valid"),
        sa.CheckConstraint(
            "rate_bp_snapshot IS NULL OR rate_bp_snapshot BETWEEN 0 AND 10000",
            name="rate_bp_snapshot_range",
        ),
        sa.CheckConstraint(
            "(basis_snapshot = 'PER_EVENT' AND fixed_amount_minor_snapshot IS NOT NULL "
            "AND rate_bp_snapshot IS NULL)"
            " OR (basis_snapshot <> 'PER_EVENT' AND rate_bp_snapshot IS NOT NULL "
            "AND fixed_amount_minor_snapshot IS NULL)",
            name="exactly_one_snapshot_for_basis",
        ),
    )
    op.create_index(
        "ix_commission_event_tenant_id_occurred_on",
        "commission_event",
        ["tenant_id", "occurred_on"],
    )

    # -------------------------------------------------- commission_adjustment
    op.create_table(
        "commission_adjustment",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("commission_event_id", UUID, nullable=False),
        # Signed: a reduced service or a voided payment moves commission down.
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column(
            "created_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=True
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "commission_event_id"],
            ["commission_event.tenant_id", "commission_event.id"],
            name="fk_commission_adjustment_tenant_id_commission_event_id",
        ),
        # COM-5: one source fact yields at most one adjustment.
        sa.UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            name="uq_commission_adjustment_tenant_id_source_type_source_id",
        ),
        sa.CheckConstraint(f"source_type IN {_SOURCE_TYPE}", name="source_type_valid"),
        # AUD-6: a reason is mandatory on every correction, void and reversal.
        sa.CheckConstraint("length(btrim(reason)) > 0", name="reason_not_blank"),
    )
    op.create_index(
        "ix_commission_adjustment_tenant_id_commission_event_id",
        "commission_adjustment",
        ["tenant_id", "commission_event_id"],
    )

    # -------------------------------------------------- commission_settlement
    # COM-6/COM-11: no settlement_id anywhere else, and no allocation table.
    op.create_table(
        "commission_settlement",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("settled_on", sa.Date, nullable=False),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("period_end >= period_start", name="period_ordered"),
        # A settlement is money that moved from the tenant to the platform, so it
        # is strictly positive. A negative row would be a commission adjustment
        # wearing a settlement's clothes: it would move outstanding without any
        # snapshotted terms, any link to an earning event, or any source fact —
        # precisely the traceability COM-4 exists to guarantee.
        #
        # This does *not* block over-settlement: settling 1200 against 1000 earned
        # is a positive row that drives outstanding to -200 (A-COM-6b). The sign
        # of the row and the sign of the aggregate are different questions.
        sa.CheckConstraint("amount_minor > 0", name="amount_positive"),
    )
    op.create_index(
        "ix_commission_settlement_tenant_id_settled_on",
        "commission_settlement",
        ["tenant_id", "settled_on"],
    )

    # --------------------------------------------------------- immutability
    op.execute(
        """
        CREATE FUNCTION commission_row_is_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'commission history is immutable (COM-3, COM-6): % rejected on %',
                TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in _IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION commission_row_is_immutable()
            """
        )


def downgrade() -> None:
    for table in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.drop_table("commission_settlement")
    op.drop_table("commission_adjustment")
    op.drop_table("commission_event")
    op.drop_table("commission_plan")
    op.execute("DROP FUNCTION IF EXISTS commission_row_is_immutable()")
    # btree_gist is left installed: it is a database-wide facility that other
    # objects may come to rely on, and dropping it is not this migration's call.
