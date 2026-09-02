"""Schema assertion against the LIVE migrated PostgreSQL database.

P0 definition-of-done item 3: "every constraint named in the architecture freeze
exists in the database — verified by a schema assertion test, not by reading the
migration file". Reading the Alembic file would only prove the file says so.

Covers A-SEC-1/2 (tenant_id NOT NULL, composite FKs), SYN-4 (partial unique
index), FIN-1/FIN-2 (column types), SEC-7 (no customer credentials), and the
scope guard that no future-package table has crept into P1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db_models import ALL_TABLES

pytestmark = pytest.mark.postgres


def _rows(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


# --- table inventory --------------------------------------------------------


class TestTableInventory:
    def test_exactly_the_expected_tables_exist(self, engine):
        rows = _rows(
            engine,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1",
        )
        actual = {r[0] for r in rows} - {"alembic_version"}
        assert actual == set(ALL_TABLES), (
            f"unexpected: {sorted(actual - set(ALL_TABLES))}, "
            f"missing: {sorted(set(ALL_TABLES) - actual)}"
        )

    @pytest.mark.parametrize(
        "forbidden",
        [
            # Later packages — must not exist yet.
            "reminder",
            "communication_log",
            "commission_plan",
            "commission_event",
            "commission_adjustment",
            "commission_settlement",
            "job_run",
            # Removed from scope entirely / never to exist.
            "payment_attempt",
            "payment_provider",
            "voice_transcript",
            "audio_recording",
            "transcript",
            "customer_login",
            "customer_credential",
            "operator",
        ],
    )
    def test_no_future_or_forbidden_table_exists(self, engine, forbidden):
        exists = _scalar(
            engine,
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename = :t",
            t=forbidden,
        )
        assert exists == 0, f"table {forbidden!r} must not exist yet"

    def test_shared_row_version_sequence_exists(self, engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_sequences "
                "WHERE schemaname='public' AND sequencename='row_version_seq'",
            )
            == 1
        )

    def test_row_version_sequence_is_shared_by_all_versioned_tables(self, engine):
        rows = _rows(
            engine,
            """
            SELECT table_name FROM information_schema.columns
            WHERE table_schema='public' AND column_name='row_version'
            ORDER BY 1
            """,
        )
        versioned = {r[0] for r in rows}
        assert versioned == {
            "tenant",
            "customer",
            "daily_service_record",
            "ledger_entry",
            # P2: authoritative records the client holds offline (P0 7.1, 7.4).
            "payment",
            "statement",
        }
        for table in versioned:
            default = _scalar(
                engine,
                """
                SELECT column_default FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:t AND column_name='row_version'
                """,
                t=table,
            )
            assert "row_version_seq" in (default or ""), (
                f"{table}.row_version must draw from the shared sequence"
            )


# --- SEC-1 / SEC-2 ----------------------------------------------------------

TENANT_OWNED = [
    "customer",
    "daily_service_record",
    "ledger_entry",
    "sync_operation",
    "billing_cycle",
    "statement",
    "payment",
]


class TestSEC1TenantColumns:
    @pytest.mark.parametrize("table", TENANT_OWNED)
    def test_SEC1_tenant_id_not_null(self, engine, table):
        nullable = _scalar(
            engine,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name='tenant_id'
            """,
            t=table,
        )
        assert nullable == "NO", f"{table}.tenant_id must be NOT NULL"

    @pytest.mark.parametrize("table", ["audit_event", "app_user", "user_session"])
    def test_SEC1_tenant_id_present_but_nullable_for_platform_rows(self, engine, table):
        """These legitimately carry platform-scope rows, so tenant_id is nullable."""
        found = _scalar(
            engine,
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name='tenant_id'
            """,
            t=table,
        )
        assert found == 1


class TestSEC2CompositeForeignKeys:
    """Every FK between business tables includes tenant_id."""

    @pytest.mark.parametrize(
        "constraint,table",
        [
            ("fk_daily_service_record_tenant_id_customer_id", "daily_service_record"),
            ("fk_ledger_entry_tenant_id_customer_id", "ledger_entry"),
            ("fk_daily_service_record_tenant_id_corrects_id", "daily_service_record"),
            ("fk_daily_service_record_tenant_id_superseded_by_id", "daily_service_record"),
            ("fk_user_session_tenant_id_user_id", "user_session"),
        ],
    )
    def test_SEC2_composite_fk_exists_and_includes_tenant_id(self, engine, constraint, table):
        rows = _rows(
            engine,
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE c.conname = :name AND c.contype = 'f' AND t.relname = :table
            """,
            name=constraint,
            table=table,
        )
        columns = [r[0] for r in rows]
        assert columns, f"foreign key {constraint} not found on {table}"
        assert "tenant_id" in columns, f"{constraint} must include tenant_id, got {columns}"
        assert len(columns) == 2

    def test_SEC2_no_plain_customer_id_fk_bypasses_tenant(self, engine):
        """A single-column FK to customer(id) would permit cross-tenant references."""
        rows = _rows(
            engine,
            """
            SELECT c.conname, t.relname, array_length(c.conkey, 1)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_class rt ON rt.oid = c.confrelid
            WHERE c.contype = 'f' AND rt.relname = 'customer'
            """,
        )
        assert rows, "expected foreign keys referencing customer"
        for name, table, ncols in rows:
            assert ncols == 2, f"{name} on {table} references customer with {ncols} column(s)"

    @pytest.mark.parametrize(
        "table,constraint",
        [
            ("customer", "uq_customer_tenant_id_id"),
            ("daily_service_record", "uq_daily_service_record_tenant_id_id"),
            ("app_user", "uq_app_user_tenant_id_id"),
        ],
    )
    def test_SEC2_composite_unique_targets_exist(self, engine, table, constraint):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                "WHERE c.conname=:n AND t.relname=:t AND c.contype='u'",
                n=constraint,
                t=table,
            )
            == 1
        )


# --- SYN-4 ------------------------------------------------------------------


class TestSYN4PartialUniqueIndex:
    def test_SYN4_active_day_index_exists_and_is_partial(self, engine):
        definition = _scalar(
            engine,
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=:n",
            n="uq_daily_service_record_active_day",
        )
        assert definition, "the duplicate-service partial unique index is missing"
        assert "UNIQUE" in definition
        assert "WHERE" in definition and "ACTIVE" in definition
        for column in ("tenant_id", "customer_id", "service_date"):
            assert column in definition


# --- FIN-1 / FIN-2 column types ---------------------------------------------


class TestColumnTypes:
    MONEY_COLUMNS = [
        ("customer", "unit_price_minor"),
        ("daily_service_record", "unit_price_minor"),
        ("daily_service_record", "charge_minor"),
        ("daily_service_record", "adjustment_minor"),
        ("ledger_entry", "amount_minor"),
        ("tenant", "default_unit_price_minor"),
    ]
    QUANTITY_COLUMNS = [
        ("customer", "default_quantity"),
        ("daily_service_record", "quantity"),
        ("tenant", "default_quantity"),
    ]

    @pytest.mark.parametrize("table,column", MONEY_COLUMNS)
    def test_FIN1_money_is_bigint(self, engine, table, column):
        dtype = _scalar(
            engine,
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name=:c
            """,
            t=table,
            c=column,
        )
        assert dtype == "bigint", f"{table}.{column} must be BIGINT minor units, got {dtype}"

    @pytest.mark.parametrize("table,column", QUANTITY_COLUMNS)
    def test_FIN2_quantity_is_numeric_12_3(self, engine, table, column):
        row = _rows(
            engine,
            """
            SELECT data_type, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name=:c
            """,
            t=table,
            c=column,
        )[0]
        assert row[0] == "numeric" and row[1] == 12 and row[2] == 3

    def test_FIN1_no_floating_point_column_anywhere(self, engine):
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


# --- checks and uniqueness --------------------------------------------------


class TestCheckConstraints:
    @pytest.mark.parametrize(
        "constraint",
        [
            "ck_daily_service_record_quantity_non_negative",
            "ck_daily_service_record_unit_price_non_negative",
            "ck_daily_service_record_charge_non_negative",
            "ck_daily_service_record_skip_is_zero",
            "ck_daily_service_record_kind_valid",
            "ck_daily_service_record_status_valid",
            "ck_daily_service_record_input_method_valid",
            "ck_ledger_entry_amount_non_zero",
            "ck_ledger_entry_entry_kind_valid",
            "ck_customer_unit_price_non_negative",
            "ck_customer_default_quantity_non_negative",
            "ck_app_user_scope_matches_role",
            "ck_sync_operation_status_valid",
            "ck_audit_event_source_valid",
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
        ), f"CHECK constraint {constraint} is missing"

    def test_SYN2_operation_id_unique_per_tenant(self, engine):
        rows = _rows(
            engine,
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE c.conname = 'uq_sync_operation_tenant_id_operation_id'
            """,
        )
        assert {r[0] for r in rows} == {"tenant_id", "operation_id"}

    def test_ledger_source_uniqueness(self, engine):
        rows = _rows(
            engine,
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE c.conname = 'uq_ledger_entry_tenant_id_source_type_source_id_entry_kind'
            """,
        )
        assert {r[0] for r in rows} == {
            "tenant_id",
            "source_type",
            "source_id",
            "entry_kind",
        }


# --- SEC-7 ------------------------------------------------------------------


class TestSEC7NoCustomerCredentials:
    def test_SEC7_customer_has_no_credential_columns(self, engine):
        rows = _rows(
            engine,
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='customer'
            """,
        )
        columns = {r[0].lower() for r in rows}
        forbidden = {
            "password",
            "password_hash",
            "credential",
            "token",
            "refresh_token",
            "refresh_token_hash",
            "secret",
            "pin",
            "otp",
        }
        assert not (columns & forbidden), f"customer must have no credentials: {columns & forbidden}"
