"""row_version semantics — the shared sequence behind optimistic concurrency
and the future (P5) sync change feed.

P0 §6: "``row_version`` is a BIGINT drawn from one shared Postgres sequence, used
for both optimistic concurrency and the sync change feed."

Two properties must hold, and neither is proven by the migration alone:

1. every sync-relevant mutation **advances** the row's version, and
2. the values come from the shared PostgreSQL sequence — never a clock, never a
   per-table counter — so they are globally monotonic and comparable across
   tables. A P5 client holding a cursor of ``N`` must be able to ask for
   "everything with row_version > N" and miss nothing.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.ids import uuid7
from app.service.commands import (
    CorrectServiceInput,
    RecordServiceInput,
    VoidServiceInput,
    correct_service,
    record_service,
    void_service,
)
from app.service.models import DailyServiceRecord, RecordStatus
from app.sync.idempotency import execute_idempotent

pytestmark = pytest.mark.postgres

PRICE = 25000


def _sequence_value(db) -> int:
    return db.execute(text("SELECT last_value FROM row_version_seq")).scalar_one()


def _record(db, ctx, customer, **kw):
    op = uuid7()
    data = RecordServiceInput(customer_id=customer.id, **kw)
    return execute_idempotent(
        db,
        ctx,
        operation_id=op,
        op_type="service.record",
        payload={"n": str(op)},
        perform=lambda: record_service(db, ctx, data, operation_id=op),
    )


def _correct(db, ctx, record_id, quantity, reason="fix"):
    op = uuid7()
    return execute_idempotent(
        db,
        ctx,
        operation_id=op,
        op_type="service.correct",
        payload={"n": str(op)},
        perform=lambda: correct_service(
            db, ctx, record_id, CorrectServiceInput(quantity=quantity, reason=reason),
            operation_id=op,
        ),
    )


def _void(db, ctx, record_id, reason="voided"):
    op = uuid7()
    return execute_idempotent(
        db,
        ctx,
        operation_id=op,
        op_type="service.void",
        payload={"n": str(op)},
        perform=lambda: void_service(
            db, ctx, record_id, VoidServiceInput(reason=reason), operation_id=op
        ),
    )


class TestCustomerRowVersion:
    def _create(self, client, tenant, code="RV-1"):
        resp = client.post(
            "/api/v1/customers",
            json={
                "operation_id": str(uuid7()),
                "code": code,
                "name": "Versioned",
                "unit_price_minor": PRICE,
                "default_quantity": "1",
            },
            headers=tenant.auth,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["entity"]

    def _patch(self, client, tenant, customer_id, **fields):
        resp = client.patch(
            f"/api/v1/customers/{customer_id}",
            json={"operation_id": str(uuid7()), **fields},
            headers=tenant.auth,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["entity"]

    def test_create_then_two_patches_strictly_advance(self, client, tenant_a):
        """create -> V1, PATCH -> V2 > V1, PATCH -> V3 > V2."""
        v1 = self._create(client, tenant_a)["row_version"]
        v2 = self._patch(client, tenant_a, self._codes(client, tenant_a), name="Second")[
            "row_version"
        ]
        assert v2 > v1
        v3 = self._patch(client, tenant_a, self._codes(client, tenant_a), name="Third")[
            "row_version"
        ]
        assert v3 > v2

    def _codes(self, client, tenant):
        items = client.get("/api/v1/customers", headers=tenant.auth).json()["items"]
        return items[0]["id"]

    def test_every_mutated_field_advances_the_version(self, client, tenant_a):
        entity = self._create(client, tenant_a, code="RV-2")
        previous = entity["row_version"]
        for field, value in (
            ("name", "Renamed"),
            ("area", "F-8"),
            ("unit_price_minor", 30000),
            ("default_quantity", "2.5"),
            ("status", "INACTIVE"),
        ):
            updated = self._patch(client, tenant_a, entity["id"], **{field: value})
            assert updated["row_version"] > previous, f"{field} did not advance row_version"
            previous = updated["row_version"]

    def test_version_comes_from_the_shared_sequence(self, client, db, tenant_a):
        """The value must be a sequence draw, not a timestamp or a counter."""
        before = _sequence_value(db)
        entity = self._create(client, tenant_a, code="RV-3")
        after = _sequence_value(db)
        assert before < entity["row_version"] <= after

    def test_optimistic_concurrency_uses_the_version(self, client, tenant_a):
        entity = self._create(client, tenant_a, code="RV-4")
        resp = client.patch(
            f"/api/v1/customers/{entity['id']}",
            json={
                "operation_id": str(uuid7()),
                "name": "Stale write",
                "expected_row_version": entity["row_version"] - 1,
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ROW_VERSION_CONFLICT"


class TestServiceRecordRowVersion:
    def test_record_draws_from_the_shared_sequence(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        before = _sequence_value(db)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        after = _sequence_value(db)
        assert before < outcome.result["row_version"] <= after

    def test_correction_advances_the_superseded_row(self, db, tenant_a, customer_factory):
        """ACTIVE -> SUPERSEDED is a sync-relevant change and must be visible."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        original_version = first.result["row_version"]

        _correct(db, tenant_a.ctx, first.entity_id, Decimal("2"))

        original = db.get(DailyServiceRecord, first.entity_id)
        db.refresh(original)
        assert original.status == RecordStatus.SUPERSEDED
        assert original.row_version > original_version, (
            "the superseded row changed but its row_version did not advance; "
            "a P5 sync cursor would never see the supersession"
        )

    def test_replacement_is_newer_than_the_row_it_supersedes(
        self, db, tenant_a, customer_factory
    ):
        """The replacement must sort AFTER the supersession in the shared sequence.

        Otherwise a client that pulls "everything after the superseded row" would
        receive the supersession without the replacement that explains it.
        """
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        second = _correct(db, tenant_a.ctx, first.entity_id, Decimal("2"))

        original = db.get(DailyServiceRecord, first.entity_id)
        replacement = db.get(DailyServiceRecord, second.entity_id)
        db.refresh(original)
        db.refresh(replacement)

        assert replacement.row_version > original.row_version

    def test_void_advances_the_row_version(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        before = first.result["row_version"]

        _void(db, tenant_a.ctx, first.entity_id)

        record = db.get(DailyServiceRecord, first.entity_id)
        db.refresh(record)
        assert record.status == RecordStatus.VOIDED
        assert record.row_version > before

    def test_correction_chain_versions_are_strictly_increasing(
        self, db, tenant_a, customer_factory
    ):
        """Walking a chain, every row's version orders consistently with events."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        r1 = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        r2 = _correct(db, tenant_a.ctx, r1.entity_id, Decimal("2"))
        r3 = _correct(db, tenant_a.ctx, r2.entity_id, Decimal("1"))

        rows = [db.get(DailyServiceRecord, x.entity_id) for x in (r1, r2, r3)]
        for row in rows:
            db.refresh(row)
        versions = [r.row_version for r in rows]
        assert versions == sorted(versions), f"chain versions out of order: {versions}"
        assert len(set(versions)) == 3


class TestSequenceIsGlobal:
    def test_versions_are_unique_across_tables(self, db, client, tenant_a, customer_factory):
        """One shared sequence: a value is never reused by another table.

        This is what lets a P5 cursor be a single number rather than one per table.
        """
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))

        customer_versions = [
            r for r in db.execute(text("SELECT row_version FROM customer")).scalars()
        ]
        record_versions = [
            r for r in db.execute(text("SELECT row_version FROM daily_service_record")).scalars()
        ]
        ledger_versions = [
            r for r in db.execute(text("SELECT row_version FROM ledger_entry")).scalars()
        ]
        all_versions = customer_versions + record_versions + ledger_versions
        assert len(all_versions) == len(set(all_versions)), "shared sequence value reused"

    def test_sequence_advances_monotonically(self, db):
        first = db.execute(text("SELECT nextval('row_version_seq')")).scalar_one()
        second = db.execute(text("SELECT nextval('row_version_seq')")).scalar_one()
        assert second > first

    def test_ledger_entries_are_versioned_too(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        versions = list(
            db.execute(text("SELECT row_version FROM ledger_entry")).scalars()
        )
        assert versions and all(v > 0 for v in versions)


class TestP2AuthoritativeRecordsAreVersioned:
    """P0 §7.1 puts payment history and statements in the client's authoritative
    offline snapshot, and §7.4 pages that snapshot on ``row_version > since``.

    Both therefore carry their own value from the shared sequence. A ledger
    row_version is **not** a substitute: the ledger entry a payment posts is a
    different record, and a client pulling payment history cannot page on it.
    """

    def test_a_payment_is_versioned_on_creation(self, db, tenant_a, customer_factory):
        from tests._ops import do_pay

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        before = _sequence_value(db)
        outcome = do_pay(db, tenant_a.ctx, customer, 5000)
        payment = _payment(db, outcome.result["id"])
        assert payment.row_version > before
        # The client reads it from the API, not only from the row.
        assert outcome.result["row_version"] == payment.row_version

    def test_a_later_payment_takes_a_higher_version(
        self, db, tenant_a, customer_factory
    ):
        from tests._ops import do_pay

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _payment(db, do_pay(db, tenant_a.ctx, customer, 100).result["id"])
        second = _payment(db, do_pay(db, tenant_a.ctx, customer, 200).result["id"])
        assert second.row_version > first.row_version

    def test_voiding_advances_that_payments_version(
        self, db, tenant_a, customer_factory
    ):
        """The one permitted mutation must be visible on the next delta."""
        from tests._ops import do_pay, do_void_payment

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = do_pay(db, tenant_a.ctx, customer, 5000)
        payment = _payment(db, outcome.result["id"])
        at_creation = payment.row_version

        voided = do_void_payment(db, tenant_a.ctx, outcome.result["id"], reason="bounced")
        db.expire_all()
        payment = _payment(db, outcome.result["id"])
        assert payment.row_version > at_creation
        assert voided.result["row_version"] == payment.row_version

    def test_the_compensating_entry_takes_its_own_later_value(
        self, db, tenant_a, customer_factory
    ):
        """The same ordering rule P1 fixed for corrections: the row whose state is
        ending is versioned first, so the entry explaining it always sorts later."""
        from app.billing.models import EntryKind, LedgerEntry
        from tests._ops import do_pay, do_void_payment

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = do_pay(db, tenant_a.ctx, customer, 5000)
        do_void_payment(db, tenant_a.ctx, outcome.result["id"], reason="bounced")
        db.expire_all()

        payment = _payment(db, outcome.result["id"])
        adjustment = db.execute(
            select(LedgerEntry).where(
                LedgerEntry.entry_kind == EntryKind.ADJUSTMENT,
                LedgerEntry.source_id == payment.id,
            )
        ).scalar_one()
        assert adjustment.row_version > payment.row_version

    def test_an_issued_statement_is_versioned(self, db, tenant_a, customer_factory):
        from decimal import Decimal as _Decimal

        from app.billing.cycles import open_cycle
        from app.billing.models import Statement
        from tests._ops import close_after_period_end, do_record

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=_Decimal("2"))
        before = _sequence_value(db)
        close_after_period_end(db, tenant_a, open_cycle(db, tenant_a.ctx))

        statement = db.execute(select(Statement)).scalars().one()
        assert statement.row_version > before

    def test_later_issued_statements_advance_the_shared_sequence(
        self, db, tenant_a, customer_factory
    ):
        from datetime import date as _date, datetime as _datetime, timezone as _tz
        from decimal import Decimal as _Decimal

        from app.billing.cycles import open_cycle
        from app.billing.models import Statement
        from tests._ops import close_after_period_end, ctx_at, do_record

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        for month in (1, 2, 3):
            ctx = ctx_at(tenant_a, _datetime(2026, month, 10, 7, tzinfo=_tz.utc))
            do_record(
                db, ctx, customer, quantity=_Decimal("1"), service_date=_date(2026, month, 5)
            )
            close_after_period_end(db, tenant_a, open_cycle(db, ctx))

        versions = [
            s.row_version
            for s in db.execute(select(Statement).order_by(Statement.issued_at))
            .scalars()
            .all()
        ]
        assert len(versions) == 3
        assert versions == sorted(versions)
        assert len(set(versions)) == 3

    def test_a_statements_version_never_changes_because_the_row_cannot(
        self, db, tenant_a, customer_factory
    ):
        """FIN-8 is unaffected: the value is drawn once, at issue, and the
        database still refuses any UPDATE."""
        from decimal import Decimal as _Decimal

        from app.billing.cycles import open_cycle
        from app.billing.models import Statement
        from tests._ops import close_after_period_end, do_record

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=_Decimal("1"))
        close_after_period_end(db, tenant_a, open_cycle(db, tenant_a.ctx))
        statement = db.execute(select(Statement)).scalars().one()

        with pytest.raises(Exception) as exc:
            db.execute(
                text("UPDATE statement SET row_version = row_version + 1 WHERE id = :i"),
                {"i": str(statement.id)},
            )
        assert "immutable" in str(exc.value)
        db.rollback()

    def test_the_cursor_stays_monotonic_across_every_versioned_table(
        self, db, tenant_a, customer_factory
    ):
        """SYN-10: one shared sequence, so a P5 cursor of N can ask for everything
        greater than N across tables and miss nothing."""
        from decimal import Decimal as _Decimal

        from app.billing.cycles import open_cycle
        from app.billing.models import LedgerEntry, Statement
        from app.payments.models import Payment
        from app.service.models import DailyServiceRecord
        from tests._ops import close_after_period_end, do_pay, do_record, do_void_payment

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=_Decimal("2"))
        paid = do_pay(db, tenant_a.ctx, customer, 1000)
        do_void_payment(db, tenant_a.ctx, paid.result["id"], reason="bounced")
        close_after_period_end(db, tenant_a, open_cycle(db, tenant_a.ctx))
        db.expire_all()

        versions: list[int] = []
        for model in (DailyServiceRecord, LedgerEntry, Payment, Statement):
            versions += [
                row.row_version for row in db.execute(select(model)).scalars().all()
            ]
        # Every value is distinct: one sequence, never a per-table counter.
        assert len(versions) == len(set(versions))
        assert min(versions) >= 1

    def test_billing_cycle_is_deliberately_not_versioned(self, engine):
        """A cycle is billing scaffolding, not a client sync entity. row_version
        was added where P0 §7.1 needs it, not to every P2 table for symmetry."""
        from sqlalchemy import text as _text

        with engine.connect() as conn:
            rows = conn.execute(
                _text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND column_name='row_version'"
                )
            ).fetchall()
        versioned = {r[0] for r in rows}
        assert versioned == {
            "tenant",
            "customer",
            "daily_service_record",
            "ledger_entry",
            "payment",
            "statement",
        }
        assert "billing_cycle" not in versioned


def _payment(db, payment_id):
    from app.payments.models import Payment

    return db.execute(
        select(Payment).where(Payment.id == uuid.UUID(str(payment_id)))
    ).scalar_one()
