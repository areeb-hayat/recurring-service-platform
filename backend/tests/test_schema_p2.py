"""Schema assertion for the P2 tables, against the LIVE migrated database.

Same principle as ``test_schema.py``: reading the Alembic file would only prove
the file says so. Every constraint P0 §6 names for ``billing_cycle``,
``statement`` and ``payment`` is looked up by name in ``pg_catalog``, and so is
the ``posting_cycle_id`` foreign key that P1 deliberately left unfinished.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _rows(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _constraint_columns(engine, name: str) -> set[str]:
    rows = _rows(
        engine,
        """
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE c.conname = :name
        """,
        name=name,
    )
    return {r[0] for r in rows}


class TestP2TablesExist:
    @pytest.mark.parametrize("table", ["billing_cycle", "statement", "payment"])
    def test_table_exists(self, engine, table):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename=:t",
                t=table,
            )
            == 1
        )

    def test_the_p2_migration_is_in_the_applied_chain(self, engine):
        """P2's revision is an ancestor of whatever the current head is.

        Deliberately not "the head *is* P2": later packages move the head, and a
        test that pins it would fail every time one legitimately does. What must
        stay true is that the applied chain still runs through P2 — so a future
        migration cannot quietly drop it and leave these tables unexplained.
        """
        import os

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = Config(os.path.join(root, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(root, "alembic"))
        script = ScriptDirectory.from_config(cfg)

        current = _scalar(engine, "SELECT version_num FROM alembic_version")
        ancestry = {rev.revision for rev in script.walk_revisions("base", current)}
        assert "0002_p2_financial_engine" in ancestry


class TestPostingCycleForeignKey:
    """The P1 boundary, closed: composite, so it cannot cross tenants (SEC-2)."""

    def test_the_composite_fk_exists_and_includes_tenant_id(self, engine):
        columns = _constraint_columns(engine, "fk_ledger_entry_tenant_id_posting_cycle_id")
        assert columns == {"tenant_id", "posting_cycle_id"}

    def test_it_targets_billing_cycle(self, engine):
        target = _scalar(
            engine,
            """
            SELECT rt.relname FROM pg_constraint c
            JOIN pg_class rt ON rt.oid = c.confrelid
            WHERE c.conname = 'fk_ledger_entry_tenant_id_posting_cycle_id'
            """,
        )
        assert target == "billing_cycle"

    def test_a_cross_tenant_posting_cycle_is_refused(self, db, tenant_a, tenant_b, customer_factory):
        from app.billing.cycles import ensure_open_cycle

        customer_a = customer_factory(tenant_a.ctx, code="XC", price_minor=1000)
        cycle_b = ensure_open_cycle(db, tenant_b.ctx)
        db.commit()
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO ledger_entry
                      (id, tenant_id, customer_id, entry_kind, amount_minor, occurred_on,
                       posting_cycle_id, source_type, source_id, created_at)
                    VALUES (gen_random_uuid(), :ta, :ca, 'CHARGE', 100, CURRENT_DATE,
                            :cycle_b, 'daily_service_record', gen_random_uuid(), now())
                    """
                ),
                {
                    "ta": str(tenant_a.ctx.tenant_id),
                    "ca": str(customer_a.id),
                    "cycle_b": str(cycle_b.id),
                },
            )
        assert "fk_ledger_entry_tenant_id_posting_cycle_id" in str(exc.value)
        db.rollback()


class TestBillingCycleConstraints:
    @pytest.mark.parametrize(
        "constraint",
        [
            "ck_billing_cycle_period_ordered",
            "ck_billing_cycle_status_valid",
            "ck_billing_cycle_closed_at_matches_status",
        ],
    )
    def test_check_constraint_exists(self, engine, constraint):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname=:n AND contype='c'",
                n=constraint,
            )
            == 1
        ), f"CHECK {constraint} is missing"

    def test_unique_period_start_per_tenant(self, engine):
        assert _constraint_columns(engine, "uq_billing_cycle_tenant_id_period_start") == {
            "tenant_id",
            "period_start",
        }

    def test_composite_unique_target_for_child_foreign_keys(self, engine):
        assert _constraint_columns(engine, "uq_billing_cycle_tenant_id_id") == {
            "tenant_id",
            "id",
        }

    def test_the_one_open_cycle_index_is_unique_and_partial(self, engine):
        definition = _scalar(
            engine,
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=:n",
            n="uq_billing_cycle_one_open_per_tenant",
        )
        assert definition, "the one-open-cycle partial unique index is missing"
        assert "UNIQUE" in definition
        assert "WHERE" in definition and "OPEN" in definition
        assert "tenant_id" in definition


class TestStatementConstraints:
    @pytest.mark.parametrize(
        "constraint",
        [
            "ck_statement_balance_identity",
            "ck_statement_charges_non_negative",
            "ck_statement_payments_non_negative",
            "ck_statement_payment_reversals_non_negative",
            "ck_statement_service_days_non_negative",
            "ck_statement_total_quantity_non_negative",
        ],
    )
    def test_check_constraint_exists(self, engine, constraint):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname=:n AND contype='c'",
                n=constraint,
            )
            == 1
        ), f"CHECK {constraint} is missing"

    def test_unique_per_customer_and_cycle(self, engine):
        assert _constraint_columns(
            engine, "uq_statement_tenant_id_customer_id_cycle_id"
        ) == {"tenant_id", "customer_id", "cycle_id"}

    @pytest.mark.parametrize(
        "constraint",
        ["fk_statement_tenant_id_customer_id", "fk_statement_tenant_id_cycle_id"],
    )
    def test_SEC2_foreign_keys_are_composite(self, engine, constraint):
        columns = _constraint_columns(engine, constraint)
        assert "tenant_id" in columns and len(columns) == 2

    def test_the_immutability_trigger_exists(self, engine):
        assert (
            _scalar(
                engine,
                """
                SELECT count(*) FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                WHERE c.relname = 'statement' AND t.tgname = 'statement_immutable'
                  AND NOT t.tgisinternal
                """,
            )
            == 1
        ), "FIN-8 immutability must be enforced by the database, not by convention"

    def test_the_cycle_index_exists(self, engine):
        assert _scalar(
            engine,
            "SELECT indexdef FROM pg_indexes WHERE indexname='ix_statement_tenant_id_cycle_id'",
        )

    def test_FIN1_every_money_column_is_bigint(self, engine):
        rows = _rows(
            engine,
            """
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name='statement'
              AND column_name LIKE '%_minor'
            """,
        )
        assert len(rows) == 6  # the six movement columns, and nothing else
        assert {r[1] for r in rows} == {"bigint"}

    def test_FIN2_total_quantity_is_numeric_12_3(self, engine):
        row = _rows(
            engine,
            """
            SELECT data_type, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='statement'
              AND column_name='total_quantity'
            """,
        )[0]
        assert (row[0], row[1], row[2]) == ("numeric", 12, 3)


class TestPaymentConstraints:
    @pytest.mark.parametrize(
        "constraint",
        [
            "ck_payment_amount_positive",
            "ck_payment_method_valid",
            "ck_payment_status_valid",
            "ck_payment_source_valid",
            "ck_payment_voided_at_matches_status",
            "ck_payment_void_requires_reason_and_actor",
        ],
    )
    def test_check_constraint_exists(self, engine, constraint):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname=:n AND contype='c'",
                n=constraint,
            )
            == 1
        ), f"CHECK {constraint} is missing"

    def test_PAY4_the_customer_fk_is_composite(self, engine):
        assert _constraint_columns(engine, "fk_payment_tenant_id_customer_id") == {
            "tenant_id",
            "customer_id",
        }

    def test_the_lookup_index_exists(self, engine):
        assert _scalar(
            engine,
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname='ix_payment_tenant_id_customer_id_received_on'",
        )

    def test_PAY6_no_amount_date_natural_key_exists(self, engine):
        """Two equal payments on one day are legal. A unique index over
        (customer, received_on, amount) would be a correctness bug."""
        definitions = _rows(
            engine,
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='payment'",
        )
        for (definition,) in definitions:
            if "UNIQUE" not in definition:
                continue
            assert "amount_minor" not in definition, definition
            assert "received_on" not in definition, definition

    def test_FIN1_amount_is_bigint(self, engine):
        assert (
            _scalar(
                engine,
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name='payment'
                  AND column_name='amount_minor'
                """,
            )
            == "bigint"
        )

    def test_A_PAY_1_no_provider_shaped_column_exists(self, engine):
        rows = _rows(
            engine,
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='payment'
            """,
        )
        columns = {r[0].lower() for r in rows}
        expected = {
            "id",
            "tenant_id",
            "customer_id",
            "amount_minor",
            "method",
            "received_on",
            "reference",
            "note",
            "status",
            "voided_reason",
            "voided_by_user_id",
            "voided_at",
            "operation_id",
            "recorded_by_user_id",
            "source",
            "recorded_at",
            "row_version",
        }
        assert columns == expected


class TestVersionedForOfflineSync:
    """P0 7.1 puts statements and payment history in the client snapshot, and
    7.4 pages it on row_version. Both tables therefore carry the column, drawn
    from the shared P1 sequence rather than a per-table counter."""

    @pytest.mark.parametrize("table", ["statement", "payment"])
    def test_row_version_is_bigint_from_the_shared_sequence(self, engine, table):
        row = _rows(
            engine,
            """
            SELECT data_type, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name='row_version'
            """,
            t=table,
        )
        assert row, f"{table}.row_version is missing"
        data_type, default, nullable = row[0]
        assert data_type == "bigint"
        assert nullable == "NO"
        assert "row_version_seq" in (default or ""), (
            f"{table}.row_version must draw from the shared sequence, got {default!r}"
        )

    def test_billing_cycle_has_no_row_version(self, engine):
        """Not added for symmetry: a cycle is not a client sync entity."""
        assert (
            _scalar(
                engine,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema='public' AND table_name='billing_cycle'
                  AND column_name='row_version'
                """,
            )
            == 0
        )


class TestNoFloatingPointCreptIn:
    def test_FIN1_still_no_floating_point_column_anywhere(self, engine):
        rows = _rows(
            engine,
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema='public'
              AND data_type IN ('real','double precision','float','money')
            """,
        )
        assert rows == [], f"floating-point columns found: {rows}"
