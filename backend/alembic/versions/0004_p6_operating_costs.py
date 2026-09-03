"""P6 operating costs: cost items, versioned rates, monthly usage, real invoices.

Revision ID: 0004_p6_operating_costs
Revises: 0003_p3_commission_engine
Create Date: P6 — Owner Financial Dashboard & Operating Costs

Adds exactly the four ``operating_cost_*`` tables and nothing else. Reminder,
communication_log and job_run remain absent — they belong to later packages.

**Nothing else in the schema moves.** P6's other half is read-only: the owner
dashboard, the statements and payments screens and the widened sync feed are all
built on tables P1–P3 already created. No column is added to ``ledger_entry``,
``payment``, ``statement`` or any ``commission_*`` table, because operating costs
are a separate accounting concept and must not touch either the customer ledger
or platform commission.

**No ``row_version`` on any of these tables.** They are not client sync entities:
the Operating Costs screen is online-only, so a version column would quietly make
them syncable and invite a later feed reader to stream them.

**``btree_gist`` again, for the same reason as P3.** A cost item's rate ranges
must not overlap, or "the rate in force" for a month would be ambiguous — and
those terms get snapshotted onto usage rows, where an ambiguity could never be
corrected afterwards. The EXCLUDE constraint is the guarantee; application code
never pre-reads to decide. The extension is already installed by 0003; the
``IF NOT EXISTS`` here keeps this migration runnable on its own.

**Two partial unique indexes** keep exactly one ACTIVE usage row and one ACTIVE
invoice row per (cost item, month), which is what makes correction-by-supersede
safe under concurrency: the index decides the winner, not a pre-read.

Written by hand for the same reason as every earlier migration: every constraint
has an explicit, stable name so the schema-assertion test can look it up rather
than trusting this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_p6_operating_costs"
down_revision = "0003_p3_commission_engine"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)

_STATUS = "('ACTIVE','SUPERSEDED')"
_RECURRENCE = "('MONTHLY','ANNUAL')"


def upgrade() -> None:
    # Required by the rate exclusion constraint below; installed by 0003 already.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # --------------------------------------------------- operating_cost_item
    op.create_table(
        "operating_cost_item",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        # Composite target for every (tenant_id, cost_item_id) child key (SEC-2).
        sa.UniqueConstraint("tenant_id", "id", name="uq_operating_cost_item_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "code", name="uq_operating_cost_item_tenant_id_code"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED')", name="status_valid"
        ),
    )

    # --------------------------------------------------- operating_cost_rate
    op.create_table(
        "operating_cost_rate",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cost_item_id", UUID, nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date, nullable=True),
        sa.Column("unit", sa.String(40), nullable=True),
        sa.Column("unit_price_minor", sa.BigInteger, nullable=True),
        sa.Column("fixed_amount_minor", sa.BigInteger, nullable=True),
        sa.Column("fixed_recurrence", sa.String(16), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("currency_exponent", sa.SmallInteger, nullable=False),
        sa.Column("source_note", sa.Text, nullable=True),
        sa.Column(
            "created_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_operating_cost_rate_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cost_item_id"],
            ["operating_cost_item.tenant_id", "operating_cost_item.id"],
            name="fk_operating_cost_rate_tenant_id_cost_item_id",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        # Exactly one pricing shape: priced per unit of usage, or a fixed charge.
        sa.CheckConstraint(
            "(unit_price_minor IS NOT NULL) <> (fixed_amount_minor IS NOT NULL)",
            name="exactly_one_pricing_shape",
        ),
        sa.CheckConstraint(
            "(unit_price_minor IS NULL) OR (unit IS NOT NULL AND unit_price_minor >= 0)",
            name="usage_rate_complete",
        ),
        sa.CheckConstraint(
            f"(fixed_amount_minor IS NULL) OR (fixed_recurrence IN {_RECURRENCE} "
            "AND fixed_amount_minor >= 0)",
            name="fixed_rate_complete",
        ),
        sa.CheckConstraint(
            "fixed_amount_minor IS NOT NULL OR fixed_recurrence IS NULL",
            name="recurrence_only_on_fixed",
        ),
        sa.CheckConstraint(
            "currency_exponent BETWEEN 0 AND 4",
            name="currency_exponent_valid",
        ),
    )
    op.create_index(
        "ix_operating_cost_rate_tenant_id_cost_item_id_effective_from",
        "operating_cost_rate",
        ["tenant_id", "cost_item_id", "effective_from"],
    )
    # A NULL effective_to is an unbounded upper bound, so an open-ended rate
    # excludes every later one until its successor closes it. This *is* the
    # guarantee that a month has one rate.
    op.execute(
        """
        ALTER TABLE operating_cost_rate
        ADD CONSTRAINT ex_operating_cost_rate_effective_range_no_overlap
        EXCLUDE USING gist (
            tenant_id WITH =,
            cost_item_id WITH =,
            daterange(effective_from, effective_to, '[]') WITH &&
        )
        """
    )

    # -------------------------------------------------- operating_cost_usage
    op.create_table(
        "operating_cost_usage",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cost_item_id", UUID, nullable=False),
        sa.Column("rate_id", UUID, nullable=False),
        sa.Column("period_month", sa.Date, nullable=False),
        # Measured usage, not a billed quantity: audio hours, GB-months, millions
        # of tokens. Exact, and never a float (FIN-1).
        sa.Column("usage_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("usage_unit", sa.String(40), nullable=False),
        sa.Column("unit_price_minor_snapshot", sa.BigInteger, nullable=False),
        sa.Column("estimated_amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("currency_exponent", sa.SmallInteger, nullable=False),
        sa.Column("inputs", postgresql.JSONB, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("supersedes_id", UUID, nullable=True),
        sa.Column("superseded_by_id", UUID, nullable=True),
        sa.Column("correction_reason", sa.Text, nullable=True),
        sa.Column(
            "recorded_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("recorded_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_operating_cost_usage_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cost_item_id"],
            ["operating_cost_item.tenant_id", "operating_cost_item.id"],
            name="fk_operating_cost_usage_tenant_id_cost_item_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rate_id"],
            ["operating_cost_rate.tenant_id", "operating_cost_rate.id"],
            name="fk_operating_cost_usage_tenant_id_rate_id",
        ),
        sa.CheckConstraint(
            "EXTRACT(DAY FROM period_month) = 1",
            name="period_is_month_start",
        ),
        sa.CheckConstraint(
            "usage_quantity >= 0", name="usage_non_negative"
        ),
        sa.CheckConstraint(
            "unit_price_minor_snapshot >= 0",
            name="unit_price_non_negative",
        ),
        sa.CheckConstraint(
            "estimated_amount_minor >= 0",
            name="estimate_non_negative",
        ),
        sa.CheckConstraint(
            f"status IN {_STATUS}", name="status_valid"
        ),
        sa.CheckConstraint(
            "currency_exponent BETWEEN 0 AND 4",
            name="currency_exponent_valid",
        ),
        # AUD-6: a superseded figure always says why it was replaced.
        sa.CheckConstraint(
            "status <> 'SUPERSEDED' OR (superseded_by_id IS NOT NULL "
            "AND correction_reason IS NOT NULL)",
            name="superseded_requires_reason",
        ),
    )
    op.create_index(
        "ix_operating_cost_usage_tenant_id_period_month",
        "operating_cost_usage",
        ["tenant_id", "period_month"],
    )
    # One ACTIVE figure per item per month; superseded history is unlimited.
    op.create_index(
        "uq_operating_cost_usage_active_period",
        "operating_cost_usage",
        ["tenant_id", "cost_item_id", "period_month"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # ------------------------------------------------- operating_cost_actual
    op.create_table(
        "operating_cost_actual",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cost_item_id", UUID, nullable=False),
        sa.Column("period_month", sa.Date, nullable=False),
        # Zero is legal: a bundled first-year domain really does cost nothing,
        # and the owner must be able to record that rather than leave it blank.
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("currency_exponent", sa.SmallInteger, nullable=False),
        sa.Column("invoice_reference", sa.String(120), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("supersedes_id", UUID, nullable=True),
        sa.Column("superseded_by_id", UUID, nullable=True),
        sa.Column("correction_reason", sa.Text, nullable=True),
        sa.Column(
            "recorded_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("recorded_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_operating_cost_actual_tenant_id_id"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cost_item_id"],
            ["operating_cost_item.tenant_id", "operating_cost_item.id"],
            name="fk_operating_cost_actual_tenant_id_cost_item_id",
        ),
        sa.CheckConstraint(
            "EXTRACT(DAY FROM period_month) = 1",
            name="period_is_month_start",
        ),
        sa.CheckConstraint(
            "amount_minor >= 0", name="amount_non_negative"
        ),
        sa.CheckConstraint(
            f"status IN {_STATUS}", name="status_valid"
        ),
        sa.CheckConstraint(
            "currency_exponent BETWEEN 0 AND 4",
            name="currency_exponent_valid",
        ),
        sa.CheckConstraint(
            "status <> 'SUPERSEDED' OR (superseded_by_id IS NOT NULL "
            "AND correction_reason IS NOT NULL)",
            name="superseded_requires_reason",
        ),
    )
    op.create_index(
        "ix_operating_cost_actual_tenant_id_period_month",
        "operating_cost_actual",
        ["tenant_id", "period_month"],
    )
    op.create_index(
        "uq_operating_cost_actual_active_period",
        "operating_cost_actual",
        ["tenant_id", "cost_item_id", "period_month"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_table("operating_cost_actual")
    op.drop_table("operating_cost_usage")
    op.execute(
        "ALTER TABLE operating_cost_rate "
        "DROP CONSTRAINT IF EXISTS ex_operating_cost_rate_effective_range_no_overlap"
    )
    op.drop_table("operating_cost_rate")
    op.drop_table("operating_cost_item")
    # btree_gist stays installed: 0003 needs it, and it is a database-wide
    # facility that is not this migration's to remove.
