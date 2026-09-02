"""P2 financial engine: billing cycles, statements, manual payments.

Revision ID: 0002_p2_financial_engine
Revises: 0001_p1_baseline
Create Date: P2 — Financial Engine

Adds exactly three tables — ``billing_cycle``, ``statement``, ``payment`` — and
finishes ``ledger_entry.posting_cycle_id``, which P1 left nullable with no
foreign key because ``billing_cycle`` did not exist yet.

``statement`` and ``payment`` carry ``row_version`` from the shared P1 sequence.
P0 §7.1 lists statements and payment history among the authoritative records the
client holds offline and §7.4 pages that snapshot on ``row_version > since``, so
these rows need their own cursor values. ``billing_cycle`` does not get one:
it is server-side billing scaffolding, not a client sync entity.

Written by hand for the same reason as the baseline: every constraint named in
P0 §6 is explicit here and its name is stable, so the schema-assertion test can
look it up rather than trusting this file.

**No backfill of ``posting_cycle_id``.** P1 wrote every entry with NULL and the
package was never deployed, so there is no historical row to place, and inventing
a cycle for one would be fabricating financial history. The column therefore
stays nullable and statement issue refuses while any NULL row exists
(``app/billing/statements.py``), which fails closed instead of quietly omitting
the entry from a bill. If a P1 database exists anywhere, assign those entries
before closing a cycle on it.

Reminder, communication_log, commission_* and job_run remain absent — they belong
to later packages.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_p2_financial_engine"
down_revision = "0001_p1_baseline"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
# The same shared sequence P1 created. ``statement`` and ``payment`` join the
# versioned set because P0 §7.1 puts statements and payment history in the
# client's authoritative offline snapshot and §7.4 pages it on row_version.
# ``billing_cycle`` deliberately does not: it is billing scaffolding, not a
# record the client syncs.
ROW_VERSION_DEFAULT = sa.text("nextval('row_version_seq')")


def upgrade() -> None:
    # ----------------------------------------------------------- billing_cycle
    op.create_table(
        "billing_cycle",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("closed_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        # Composite target for (tenant_id, posting_cycle_id) and (tenant_id, cycle_id).
        sa.UniqueConstraint("tenant_id", "id", name="uq_billing_cycle_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "period_start", name="uq_billing_cycle_tenant_id_period_start"
        ),
        sa.CheckConstraint("period_end >= period_start", name="period_ordered"),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)",
            name="closed_at_matches_status",
        ),
    )
    # P0 §5.5: exactly one OPEN cycle per tenant. This partial unique index *is*
    # the guarantee — application code never pre-reads to decide.
    op.create_index(
        "uq_billing_cycle_one_open_per_tenant",
        "billing_cycle",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    # --------------------------------------------- ledger_entry.posting_cycle_id
    # The P1 deferred boundary, closed: composite so an entry can never post into
    # another tenant's cycle (SEC-2).
    op.create_foreign_key(
        "fk_ledger_entry_tenant_id_posting_cycle_id",
        "ledger_entry",
        "billing_cycle",
        ["tenant_id", "posting_cycle_id"],
        ["tenant_id", "id"],
    )

    # --------------------------------------------------------------- statement
    op.create_table(
        "statement",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("customer_id", UUID, nullable=False),
        sa.Column("cycle_id", UUID, nullable=False),
        sa.Column("issued_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("opening_balance_minor", sa.BigInteger, nullable=False),
        sa.Column("charges_minor", sa.BigInteger, nullable=False),
        sa.Column("service_adjustments_minor", sa.BigInteger, nullable=False),
        sa.Column("payments_minor", sa.BigInteger, nullable=False),
        sa.Column("payment_reversals_minor", sa.BigInteger, nullable=False),
        sa.Column("closing_balance_minor", sa.BigInteger, nullable=False),
        sa.Column("service_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_quantity", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("unit_label", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("row_version", sa.BigInteger, nullable=False, server_default=ROW_VERSION_DEFAULT),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_statement_tenant_id_customer_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["billing_cycle.tenant_id", "billing_cycle.id"],
            name="fk_statement_tenant_id_cycle_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "customer_id",
            "cycle_id",
            name="uq_statement_tenant_id_customer_id_cycle_id",
        ),
        # FIN-8 in the database: the §5.4 identity holds even for a direct INSERT
        # that bypasses the application entirely.
        sa.CheckConstraint(
            "closing_balance_minor = opening_balance_minor + charges_minor "
            "+ service_adjustments_minor - payments_minor + payment_reversals_minor",
            name="balance_identity",
        ),
        sa.CheckConstraint("charges_minor >= 0", name="charges_non_negative"),
        sa.CheckConstraint("payments_minor >= 0", name="payments_non_negative"),
        sa.CheckConstraint(
            "payment_reversals_minor >= 0", name="payment_reversals_non_negative"
        ),
        sa.CheckConstraint("service_days >= 0", name="service_days_non_negative"),
        sa.CheckConstraint("total_quantity >= 0", name="total_quantity_non_negative"),
    )
    op.create_index("ix_statement_tenant_id_cycle_id", "statement", ["tenant_id", "cycle_id"])

    # FIN-8 / AUD-1: "fully immutable after issue" made structural. Without this,
    # immutability would rest on the absence of a route — true today, and one
    # careless UPDATE away from false. A statement is the document a customer was
    # shown; it is the one table where after-the-fact editing must be impossible
    # rather than merely unimplemented.
    op.execute(
        """
        CREATE FUNCTION statement_is_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'statement is immutable once issued (FIN-8): % rejected on statement %',
                TG_OP, COALESCE(OLD.id::text, '?');
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER statement_immutable
        BEFORE UPDATE OR DELETE ON statement
        FOR EACH ROW EXECUTE FUNCTION statement_is_immutable()
        """
    )

    # ----------------------------------------------------------------- payment
    # Manual only (PAY-1, PAY-2): no provider column, no attempt table, no
    # callback route, and no amount/date natural key (PAY-6) — two genuine equal
    # cash payments on the same day are legal and must both post.
    op.create_table(
        "payment",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("customer_id", UUID, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("received_on", sa.Date, nullable=False),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="RECORDED"),
        sa.Column("voided_reason", sa.Text, nullable=True),
        sa.Column("voided_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("voided_at", TS, nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column(
            "recorded_by_user_id", UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default="ONLINE"),
        sa.Column("recorded_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("row_version", sa.BigInteger, nullable=False, server_default=ROW_VERSION_DEFAULT),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_payment_tenant_id_customer_id",
        ),
        sa.CheckConstraint("amount_minor > 0", name="amount_positive"),
        sa.CheckConstraint(
            "method IN ('CASH','BANK_TRANSFER','OTHER')", name="method_valid"
        ),
        sa.CheckConstraint("status IN ('RECORDED','VOIDED')", name="status_valid"),
        sa.CheckConstraint("source IN ('ONLINE','SYNC','IMPORT')", name="source_valid"),
        sa.CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="voided_at_matches_status",
        ),
        sa.CheckConstraint(
            "status <> 'VOIDED' OR (voided_reason IS NOT NULL "
            "AND voided_by_user_id IS NOT NULL)",
            name="void_requires_reason_and_actor",
        ),
    )
    op.create_index(
        "ix_payment_tenant_id_customer_id_received_on",
        "payment",
        ["tenant_id", "customer_id", "received_on"],
    )


def downgrade() -> None:
    op.drop_table("payment")
    op.execute("DROP TRIGGER IF EXISTS statement_immutable ON statement")
    op.drop_table("statement")
    op.execute("DROP FUNCTION IF EXISTS statement_is_immutable()")
    op.drop_constraint(
        "fk_ledger_entry_tenant_id_posting_cycle_id", "ledger_entry", type_="foreignkey"
    )
    op.drop_table("billing_cycle")
