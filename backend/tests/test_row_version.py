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
