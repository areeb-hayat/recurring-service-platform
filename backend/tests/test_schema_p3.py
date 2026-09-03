"""Live-schema assertions for the four P3 commission tables.

P0 definition-of-done item 3: every constraint named in the architecture freeze
must exist *in the database*, verified against the migrated schema rather than by
reading the migration file.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db_models import P3_TABLES

pytestmark = pytest.mark.postgres

COMMISSION_TABLES = sorted(P3_TABLES)


def _rows(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


class TestP3TablesExist:
    @pytest.mark.parametrize("table", COMMISSION_TABLES)
    def test_table_exists(self, engine, table):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                "AND tablename = :t",
                t=table,
            )
            == 1
        )

    def test_the_p3_migration_is_in_the_applied_chain(self, engine):
        """P3's revision is an ancestor of whatever the current head is.

        Deliberately not "the head *is* P3" — which is what this asserted until
        P6 legitimately moved the head and it failed. P2's equivalent test had
        already got this right and P3's had regressed to pinning; the property
        worth protecting is that the applied chain still runs *through* P3, so a
        future migration cannot quietly drop it and leave these tables
        unexplained. Pinning the head instead just fails on every new package.
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
        assert "0003_p3_commission_engine" in ancestry

    def test_P3_adds_exactly_four_tables(self):
        assert len(P3_TABLES) == 4

    def test_btree_gist_is_installed(self, engine):
        """Required by the plan exclusion constraint; asserted so a rebuilt
        database without it fails loudly rather than losing the guarantee."""
        assert (
            _scalar(
                engine, "SELECT count(*) FROM pg_extension WHERE extname='btree_gist'"
            )
            == 1
        )


class TestSEC1AndSEC2:
    @pytest.mark.parametrize("table", COMMISSION_TABLES)
    def test_SEC1_tenant_id_is_not_null(self, engine, table):
        assert (
            _scalar(
                engine,
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'tenant_id'",
                t=table,
            )
            == "NO"
        )

    @pytest.mark.parametrize(
        "constraint,table",
        [
            ("fk_commission_event_tenant_id_plan_id", "commission_event"),
            (
                "fk_commission_adjustment_tenant_id_commission_event_id",
                "commission_adjustment",
            ),
        ],
    )
    def test_SEC2_the_cross_table_fk_is_composite(self, engine, constraint, table):
        rows = _rows(
            engine,
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conname = :name AND c.conrelid = cast(:table as regclass)
            ORDER BY k.ord
            """,
            name=constraint,
            table=table,
        )
        columns = [r[0] for r in rows]
        assert columns, f"{constraint} does not exist"
        assert "tenant_id" in columns, f"{constraint} is not tenant-scoped"

    @pytest.mark.parametrize(
        "table,constraint",
        [
            ("commission_plan", "uq_commission_plan_tenant_id_id"),
            ("commission_event", "uq_commission_event_tenant_id_id"),
        ],
    )
    def test_SEC2_composite_unique_targets_exist(self, engine, table, constraint):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname = :n "
                "AND conrelid = cast(:t as regclass)",
                n=constraint,
                t=table,
            )
            == 1
        )


class TestCOM5Uniqueness:
    @pytest.mark.parametrize(
        "table,constraint",
        [
            (
                "commission_event",
                "uq_commission_event_tenant_id_source_type_source_id",
            ),
            (
                "commission_adjustment",
                "uq_commission_adjustment_tenant_id_source_type_source_id",
            ),
        ],
    )
    def test_COM5_source_uniqueness_exists(self, engine, table, constraint):
        rows = _rows(
            engine,
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conname = :n AND c.conrelid = cast(:t as regclass) AND c.contype = 'u'
            ORDER BY k.ord
            """,
            n=constraint,
            t=table,
        )
        assert [r[0] for r in rows] == ["tenant_id", "source_type", "source_id"]


class TestCheckConstraints:
    @pytest.mark.parametrize(
        "constraint",
        [
            "ck_commission_plan_basis_valid",
            "ck_commission_plan_rate_bp_range",
            "ck_commission_plan_fixed_amount_non_negative",
            "ck_commission_plan_exactly_one_term_for_basis",
            "ck_commission_plan_effective_range_ordered",
            "ck_commission_event_basis_snapshot_valid",
            "ck_commission_event_source_type_valid",
            "ck_commission_event_rate_bp_snapshot_range",
            "ck_commission_event_exactly_one_snapshot_for_basis",
            "ck_commission_adjustment_source_type_valid",
            "ck_commission_adjustment_reason_not_blank",
            "ck_commission_settlement_period_ordered",
            "ck_commission_settlement_amount_positive",
        ],
    )
    def test_check_constraint_exists(self, engine, constraint):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname = :n AND contype='c'",
                n=constraint,
            )
            == 1
        ), f"{constraint} missing from the live schema"

    def test_the_plan_exclusion_constraint_exists_and_is_an_exclusion(self, engine):
        contype = _scalar(
            engine,
            "SELECT contype FROM pg_constraint "
            "WHERE conname = 'ex_commission_plan_effective_range_no_overlap'",
        )
        assert contype == "x", "the non-overlap guarantee is not an EXCLUDE constraint"


class TestIndexes:
    @pytest.mark.parametrize(
        "index",
        [
            "ix_commission_plan_tenant_id_effective_from",
            "ix_commission_event_tenant_id_occurred_on",
            "ix_commission_adjustment_tenant_id_commission_event_id",
            "ix_commission_settlement_tenant_id_settled_on",
        ],
    )
    def test_index_exists(self, engine, index):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
                "AND indexname = :n",
                n=index,
            )
            == 1
        )


class TestFIN1MoneyIsBigint:
    @pytest.mark.parametrize(
        "table,column",
        [
            ("commission_plan", "fixed_amount_minor"),
            ("commission_event", "fixed_amount_minor_snapshot"),
            ("commission_event", "base_amount_minor"),
            ("commission_event", "commission_minor"),
            ("commission_adjustment", "amount_minor"),
            ("commission_settlement", "amount_minor"),
        ],
    )
    def test_FIN1_money_column_is_bigint(self, engine, table, column):
        assert (
            _scalar(
                engine,
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c",
                t=table,
                c=column,
            )
            == "bigint"
        )

    def test_FIN1_no_floating_point_column_on_any_commission_table(self, engine):
        rows = _rows(
            engine,
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name LIKE 'commission%'
              AND data_type IN ('real','double precision','money')
            """,
        )
        assert rows == []


class TestNotASyncEntity:
    """P0 §6: the commission family is server-side only and carries no cursor."""

    @pytest.mark.parametrize("table", COMMISSION_TABLES)
    def test_no_row_version_column(self, engine, table):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'row_version'",
                t=table,
            )
            == 0
        ), f"{table} must not be a client sync entity"


class TestImmutabilityTriggers:
    @pytest.mark.parametrize(
        "table",
        ["commission_event", "commission_adjustment", "commission_settlement"],
    )
    def test_the_immutability_trigger_exists_for_update_and_delete(self, engine, table):
        rows = _rows(
            engine,
            """
            SELECT t.tgname, t.tgtype
            FROM pg_trigger t
            WHERE t.tgrelid = cast(:table as regclass) AND NOT t.tgisinternal
            """,
            table=table,
        )
        assert len(rows) == 1, f"{table} has {len(rows)} triggers, expected 1"
        name, tgtype = rows[0]
        assert name == f"{table}_immutable"
        # bit 4 = UPDATE, bit 3 = DELETE, bit 0 = ROW-level.
        assert tgtype & (1 << 4), f"{table} trigger does not fire on UPDATE"
        assert tgtype & (1 << 3), f"{table} trigger does not fire on DELETE"
        assert tgtype & 1, f"{table} trigger is not FOR EACH ROW"

    def test_commission_plan_is_deliberately_not_frozen(self, engine):
        """Closing an open range is a plan's one permitted lifecycle transition."""
        rows = _rows(
            engine,
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'commission_plan'::regclass AND NOT tgisinternal",
        )
        assert rows == []


class TestNoFutureOrForbiddenCommissionArtefact:
    @pytest.mark.parametrize(
        "column",
        ["settlement_id", "commission_settlement_id", "allocated_minor"],
    )
    def test_COM11_no_settlement_reference_column_exists_anywhere(self, engine, column):
        rows = _rows(
            engine,
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name = :c",
            c=column,
        )
        assert rows == [], f"{column} exists on {[r[0] for r in rows]}"

    def test_no_commission_table_references_a_customer(self, engine):
        """Commission is a tenant-level commercial arrangement, not a per-customer
        one: P0 §6 lists no customer column on any of the four tables."""
        rows = _rows(
            engine,
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name LIKE 'commission%' "
            "AND column_name = 'customer_id'",
        )
        assert rows == []
