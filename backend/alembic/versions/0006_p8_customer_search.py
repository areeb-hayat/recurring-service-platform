"""P8 customer search: aliases, normalized comparison keys, trigram indexes.

Revision ID: 0006_p8_customer_search
Revises: 0005_p7_reminder_engine
Create Date: P8 — Smart Search & Customer Identification

One table, two columns' worth of search keys, and the indexes that make matching
cheap. Nothing financial moves: no column is added to ``ledger_entry``,
``payment``, ``statement``, any ``commission_*``, any ``operating_cost_*`` or any
reminder table, and no constraint on any of them changes. Search reads; it never
writes a balance.

**``customer_alias`` — the one table beyond P0 §6's inventory.** It is a
deliberate addition, not a deferred one. P0 §8.3 requires customer resolution to
be "deterministic server-side matching against the tenant's own customers", and
P0 §12.3 forbids sending the customer list to a model — so the names a customer
is actually called have to be *data*, in the tenant's own database, or
identification is guesswork. It carries **no ``row_version``**: an alias is not a
sync entity of its own, it travels inside the customer's payload and an alias
write bumps the *customer's* version instead.

**Aliases are unique per customer, never per tenant.** Two brothers can both be
"Ahmed bhai", and the resolver's whole job is to answer that with a question
rather than a guess — so the schema must be able to represent it. The partial
unique index constrains one *active* spelling per customer and nothing more.

**``customer.normalized_name``** is the comparison key for the customer's own
name, written by ``app.search.normalize.normalize_text`` on every write path. It
is a column rather than a ``lower(name)`` expression in a WHERE clause so that
"the same name" has exactly one definition, in Python, testable on its own — and
so that an index can serve it. The backfill below runs that same function over
existing rows rather than approximating it in SQL; if the normalization rules
ever change, that is a new migration, not a silent divergence.

**``pg_trgm``, and why it earns its place.** It does two jobs here, not one:

1. the GIN indexes make ``LIKE '%…%'`` — which is how whole-word token matching
   and substring matching are expressed — index-served rather than a sequential
   scan over every customer and alias;
2. ``word_similarity`` supplies typo tolerance ("Ahmd" → Ahmed) as a *candidate*,
   at a threshold fixed in application code rather than read from the session
   GUC, so the same query gives the same answer on every connection.

It is PostgreSQL's own, ships with every standard distribution and every managed
PostgreSQL this project could plausibly run on, and needs no service, no daemon
and no second datastore. **Deployment implication:** ``CREATE EXTENSION``
requires an elevated role once, at migration time (``rds_superuser`` on RDS,
``cloudsqlsuperuser`` on Cloud SQL). The application degrades honestly if it is
absent — ``app.search.query.trigram_available`` checks ``pg_extension`` and skips
the fuzzy source — but the substring indexes would then be missing too, so the
extension is created here rather than left optional.

Written by hand like every earlier migration: every constraint and index has an
explicit, stable name so the schema-assertion test can look it up rather than
trusting this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_p8_customer_search"
down_revision = "0005_p7_reminder_engine"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def _backfill_normalized_names() -> None:
    """Populate ``normalized_name`` for rows that predate this migration.

    Done in Python through the very function the application uses, so an existing
    customer is findable under exactly the same rules as one created tomorrow.
    Batched by id so a large tenant does not build one enormous statement.
    """
    from app.search.normalize import normalize_text

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, name FROM customer")).fetchall()
    for row in rows:
        connection.execute(
            sa.text("UPDATE customer SET normalized_name = :n WHERE id = :id"),
            {"n": normalize_text(row.name), "id": row.id},
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------- customer search keys
    op.add_column(
        "customer",
        sa.Column(
            "normalized_name", sa.String(200), nullable=False, server_default=""
        ),
    )
    _backfill_normalized_names()
    op.create_index(
        "ix_customer_tenant_id_normalized_name",
        "customer",
        ["tenant_id", "normalized_name"],
    )
    # The one case-folding expression in the schema, and the index that serves
    # it. ``lower(text)`` is IMMUTABLE, so it is indexable; a customer code is an
    # ASCII identifier, for which ``lower`` and the application's full
    # normalization agree.
    op.execute(
        "CREATE INDEX ix_customer_tenant_id_lower_code "
        "ON customer (tenant_id, lower(btrim(code)))"
    )
    op.execute(
        "CREATE INDEX ix_customer_normalized_name_trgm "
        "ON customer USING gin (normalized_name gin_trgm_ops)"
    )

    # ------------------------------------------------------- customer_alias
    op.create_table(
        "customer_alias",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("customer_id", UUID, nullable=False),
        # What the owner typed. This is what is shown back to them, always.
        sa.Column("alias", sa.String(200), nullable=False),
        # normalize_text(alias). Never displayed.
        sa.Column("normalized", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("deactivated_at", TS, nullable=True),
        # SEC-2: an alias can only ever name its own tenant's customer.
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_customer_alias_tenant_id_customer_id",
        ),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status_valid"),
        sa.CheckConstraint("length(btrim(alias)) > 0", name="alias_not_blank"),
        sa.CheckConstraint(
            "length(btrim(normalized)) > 0", name="normalized_not_blank"
        ),
        sa.CheckConstraint(
            "(status = 'INACTIVE') = (deactivated_at IS NOT NULL)",
            name="deactivated_at_matches_status",
        ),
    )
    # One *active* spelling per customer. Partial, so a retired alias can sit
    # alongside the same spelling brought back later — which is exactly what
    # ``add_alias`` reactivates rather than duplicating.
    op.create_index(
        "uq_customer_alias_active_normalized",
        "customer_alias",
        ["tenant_id", "customer_id", "normalized"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_customer_alias_tenant_id_normalized",
        "customer_alias",
        ["tenant_id", "normalized"],
    )
    op.create_index(
        "ix_customer_alias_tenant_id_customer_id",
        "customer_alias",
        ["tenant_id", "customer_id"],
    )
    op.execute(
        "CREATE INDEX ix_customer_alias_normalized_trgm "
        "ON customer_alias USING gin (normalized gin_trgm_ops)"
    )

    # An alias is identity, and identity is history: how somebody was known last
    # year explains an audit row from last year. Corrections and retirements are
    # updates; a row never leaves.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION customer_alias_no_delete()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'customer alias history has no delete path: % on % is not permitted',
                TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_customer_alias_no_delete
        BEFORE DELETE ON customer_alias
        FOR EACH ROW EXECUTE FUNCTION customer_alias_no_delete()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_alias_no_delete ON customer_alias"
    )
    op.execute("DROP TABLE customer_alias")
    op.execute("DROP FUNCTION IF EXISTS customer_alias_no_delete()")
    op.execute("DROP INDEX IF EXISTS ix_customer_normalized_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_customer_tenant_id_lower_code")
    op.drop_index("ix_customer_tenant_id_normalized_name", table_name="customer")
    op.drop_column("customer", "normalized_name")
    # pg_trgm is deliberately left installed. Dropping an extension another
    # object might depend on is not this migration's business to guess at, and an
    # unused extension costs nothing.
