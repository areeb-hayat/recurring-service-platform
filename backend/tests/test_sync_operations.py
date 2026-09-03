"""P5 push sync — POST /api/v1/sync/operations.

The four verdicts (P0 §7.3), batch transaction independence, and the promise
that a synchronised operation is validated and authorised exactly like an online
one (SYN-8). Everything here goes through HTTP, because the endpoint's contract
*is* the per-operation result list.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import func, select

from app.billing.models import LedgerEntry
from app.commission.models import CommissionEvent
from app.payments.models import Payment
from app.core.ids import uuid7
from app.service.models import DailyServiceRecord
from app.sync.models import SyncOperation

pytestmark = pytest.mark.postgres

PRICE = 25000
SYNC_URL = "/api/v1/sync/operations"


def envelope(customer_id: str, **payload) -> dict:
    kind = payload.get("kind", "SERVICE")
    return {
        "operation_id": str(uuid7()),
        "op_type": "service.skip" if kind == "SKIP" else "service.record",
        "payload": {"customer_id": customer_id, **payload},
        "client_created_at": "2026-03-15T06:00:00Z",
    }


def push(client, fixture, *envelopes) -> list[dict]:
    resp = client.post(
        SYNC_URL, json={"operations": list(envelopes)}, headers=fixture.auth
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


@pytest.fixture
def customer(client, tenant_a):
    body = {
        "operation_id": str(uuid7()),
        "code": "C-001",
        "name": "Ayesha Khan",
        "unit_price_minor": PRICE,
        "default_quantity": "1",
    }
    resp = client.post("/api/v1/customers", json=body, headers=tenant_a.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["entity"]


@pytest.fixture
def other_customer(client, tenant_a):
    body = {
        "operation_id": str(uuid7()),
        "code": "C-002",
        "name": "Bilal Ahmed",
        "unit_price_minor": PRICE,
        "default_quantity": "1",
    }
    resp = client.post("/api/v1/customers", json=body, headers=tenant_a.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["entity"]


def _records(db, tenant) -> int:
    return db.execute(
        select(func.count())
        .select_from(DailyServiceRecord)
        .where(DailyServiceRecord.tenant_id == tenant.tenant.id)
    ).scalar_one()


class TestApplied:
    def test_applied_creates_the_record_and_the_ledger_charge(
        self, client, db, tenant_a, customer
    ):
        (result,) = push(client, tenant_a, envelope(customer["id"], quantity="2"))
        assert result["status"] == "APPLIED"
        assert result["entity"]["charge_minor"] == 50000
        assert result["entity"]["quantity"] == "2.000"
        assert _records(db, tenant_a) == 1
        entries = db.execute(
            select(LedgerEntry).where(LedgerEntry.tenant_id == tenant_a.tenant.id)
        ).scalars().all()
        assert [e.amount_minor for e in entries] == [50000]

    def test_source_provenance_is_SYNC(self, client, db, tenant_a, customer):
        (result,) = push(client, tenant_a, envelope(customer["id"], quantity="1"))
        assert result["entity"]["source"] == "SYNC"
        record = db.execute(select(DailyServiceRecord)).scalar_one()
        assert record.source == "SYNC"
        assert record.input_method == "BUTTON"

    def test_online_route_still_records_ONLINE(self, client, tenant_a, customer):
        """The two transports are distinguishable; only the provenance differs."""
        resp = client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": customer["id"],
                "quantity": "1",
            },
            headers=tenant_a.auth,
        )
        assert resp.json()["entity"]["source"] == "ONLINE"

    def test_register_row_is_written_with_the_effect(self, client, db, tenant_a, customer):
        """SYN-3, through the sync path."""
        env = envelope(customer["id"], quantity="1")
        push(client, tenant_a, env)
        row = db.execute(
            select(SyncOperation).where(
                SyncOperation.operation_id == uuid.UUID(env["operation_id"])
            )
        ).scalar_one()
        assert row.status == "APPLIED"
        assert row.entity_type == "daily_service_record"
        assert row.result["id"] == str(db.execute(select(DailyServiceRecord.id)).scalar_one())

    def test_skip_creates_no_ledger_entry_and_no_commission(
        self, client, db, tenant_a, customer
    ):
        """FIN-7 and COM-2 hold on the sync path exactly as they do online."""
        (result,) = push(client, tenant_a, envelope(customer["id"], kind="SKIP"))
        assert result["status"] == "APPLIED"
        assert result["entity"]["kind"] == "SKIP"
        assert result["entity"]["charge_minor"] == 0
        assert db.execute(select(func.count()).select_from(LedgerEntry)).scalar_one() == 0
        assert (
            db.execute(select(func.count()).select_from(CommissionEvent)).scalar_one() == 0
        )

    def test_commission_is_earned_server_side_on_an_accepted_service(
        self, client, db, tenant_a, customer, platform_token
    ):
        """COM-2: earned inside the accepting transaction, never by the device.

        The envelope carries no commission field of any kind — there is nowhere
        for a device to put one — and a plan in force produces the event anyway.
        """
        plan = client.post(
            "/api/v1/platform/commission/plans",
            json={
                "operation_id": str(uuid7()),
                "tenant_id": str(tenant_a.tenant.id),
                "basis": "RECORDED_VALUE",
                "rate_bp": 250,
                "effective_from": "2026-01-01",
            },
            headers={"Authorization": f"Bearer {platform_token}"},
        )
        assert plan.status_code == 201, plan.text

        (result,) = push(client, tenant_a, envelope(customer["id"], quantity="2"))
        assert result["status"] == "APPLIED"
        event = db.execute(select(CommissionEvent)).scalar_one()
        assert event.source_type == "daily_service_record"
        assert event.commission_minor == 1250  # 2.5% of 50000
        assert "commission" not in str(result["entity"]).lower()


class TestDuplicate:
    def test_replay_returns_DUPLICATE_with_the_same_entity(
        self, client, db, tenant_a, customer
    ):
        """A-SYN-6: the lost-response guarantee, over the sync endpoint."""
        env = envelope(customer["id"], quantity="2")
        (first,) = push(client, tenant_a, env)
        (second,) = push(client, tenant_a, env)
        assert first["status"] == "APPLIED"
        assert second["status"] == "DUPLICATE"
        assert second["entity"] == first["entity"]
        assert _records(db, tenant_a) == 1
        assert db.execute(select(func.count()).select_from(LedgerEntry)).scalar_one() == 1

    def test_replay_inside_one_batch_is_a_single_effect(
        self, client, db, tenant_a, customer
    ):
        env = envelope(customer["id"], quantity="2")
        results = push(client, tenant_a, env, dict(env))
        assert [r["status"] for r in results] == ["APPLIED", "DUPLICATE"]
        assert _records(db, tenant_a) == 1

    def test_five_concurrent_identical_envelopes_apply_once(
        self, app, tenant_a, customer, session_factory
    ):
        """A-SYN-1/2 and SYN-15 over HTTP: one APPLIED, four DUPLICATE.

        None may surface as CONFLICT — the register serialises replays, not the
        daily-record active-day index.
        """
        from fastapi.testclient import TestClient

        env = envelope(customer["id"], quantity="2")
        statuses: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(5)

        def worker() -> None:
            try:
                with TestClient(app, raise_server_exceptions=False) as c:
                    barrier.wait(timeout=10)
                    resp = c.post(
                        SYNC_URL, json={"operations": [env]}, headers=tenant_a.auth
                    )
                    assert resp.status_code == 200, resp.text
                    statuses.append(resp.json()["results"][0]["status"])
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, errors
        assert sorted(statuses) == ["APPLIED"] + ["DUPLICATE"] * 4

        session = session_factory()
        try:
            count = session.execute(
                select(func.count()).select_from(DailyServiceRecord)
            ).scalar_one()
        finally:
            session.close()
        assert count == 1


class TestConflict:
    def test_second_operation_id_same_customer_and_date_is_CONFLICT(
        self, client, db, tenant_a, customer
    ):
        """A-SYN-4: not a second row, not an overwrite."""
        (first,) = push(client, tenant_a, envelope(customer["id"], quantity="2"))
        (second,) = push(client, tenant_a, envelope(customer["id"], quantity="9"))
        assert first["status"] == "APPLIED"
        assert second["status"] == "CONFLICT"
        assert second["error"]["code"] == "SERVICE_ALREADY_RECORDED"
        assert _records(db, tenant_a) == 1

    def test_conflict_carries_the_authoritative_server_state(
        self, client, tenant_a, customer
    ):
        """SYN-7: the server states what it holds; it never merges or picks."""
        (first,) = push(client, tenant_a, envelope(customer["id"], quantity="2"))
        (second,) = push(client, tenant_a, envelope(customer["id"], quantity="9"))
        state = second["server_state"]
        assert state["id"] == first["entity"]["id"]
        assert state["quantity"] == "2.000"
        assert state["customer_id"] == customer["id"]
        assert state["service_date"] == first["entity"]["service_date"]
        assert "entity" not in second

    def test_conflict_does_not_register_the_operation(
        self, client, db, tenant_a, customer
    ):
        """A refused operation must not poison its own id permanently."""
        push(client, tenant_a, envelope(customer["id"], quantity="2"))
        loser = envelope(customer["id"], quantity="9")
        push(client, tenant_a, loser)
        assert (
            db.execute(
                select(func.count())
                .select_from(SyncOperation)
                .where(SyncOperation.operation_id == uuid.UUID(loser["operation_id"]))
            ).scalar_one()
            == 0
        )

    def test_same_id_different_payload_is_CONFLICT_and_applies_nothing(
        self, client, db, tenant_a, customer
    ):
        """A-SYN-14 over sync: fails closed in both directions."""
        env = envelope(customer["id"], quantity="2")
        push(client, tenant_a, env)
        mutated = {**env, "payload": {**env["payload"], "quantity": "5"}}
        (result,) = push(client, tenant_a, mutated)
        assert result["status"] == "CONFLICT"
        assert result["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"
        assert result["server_state"]["quantity"] == "2.000"
        record = db.execute(select(DailyServiceRecord)).scalar_one()
        assert str(record.quantity) == "2.000"


class TestRejected:
    def test_negative_quantity_is_REJECTED(self, client, db, tenant_a, customer):
        (result,) = push(client, tenant_a, envelope(customer["id"], quantity="-1"))
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "VALIDATION"
        assert _records(db, tenant_a) == 0

    def test_unknown_customer_is_REJECTED_as_NOT_FOUND(self, client, tenant_a):
        (result,) = push(client, tenant_a, envelope(str(uuid7()), quantity="1"))
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "NOT_FOUND"

    def test_missing_quantity_on_a_SERVICE_is_REJECTED(self, client, tenant_a, customer):
        (result,) = push(client, tenant_a, envelope(customer["id"]))
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "VALIDATION"

    def test_future_service_date_is_REJECTED(self, client, tenant_a, customer):
        (result,) = push(
            client, tenant_a, envelope(customer["id"], quantity="1", service_date="2099-01-01")
        )
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "VALIDATION"
        assert "service_date" in result["error"]["field_errors"]

    def test_malformed_payload_is_REJECTED_not_a_500(self, client, tenant_a):
        (result,) = push(
            client,
            tenant_a,
            {
                "operation_id": str(uuid7()),
                "op_type": "service.record",
                "payload": {"customer_id": "not-a-uuid", "quantity": "1"},
            },
        )
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "VALIDATION"
        assert "customer_id" in result["error"]["field_errors"]

    def test_unknown_field_in_payload_is_REJECTED(self, client, tenant_a, customer):
        (result,) = push(
            client,
            tenant_a,
            {
                "operation_id": str(uuid7()),
                "op_type": "service.record",
                "payload": {
                    "customer_id": customer["id"],
                    "quantity": "1",
                    "charge_minor": 1,
                },
            },
        )
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "VALIDATION"

    def test_rejection_does_not_register_the_operation(self, client, db, tenant_a, customer):
        """A transient rejection must not permanently burn the operation_id."""
        env = envelope(customer["id"], quantity="-1")
        push(client, tenant_a, env)
        assert (
            db.execute(select(func.count()).select_from(SyncOperation)).scalar_one() == 1
        )  # only the customer create

    def test_out_of_scope_op_type_is_REJECTED(self, client, tenant_a, customer):
        """V1 guarantees offline CONFIRM and SKIP. Nothing else may be queued."""
        for op_type in ("payment.record", "customer.update", "service.correct"):
            (result,) = push(
                client,
                tenant_a,
                {
                    "operation_id": str(uuid7()),
                    "op_type": op_type,
                    "payload": {"customer_id": customer["id"], "quantity": "1"},
                },
            )
            assert result["status"] == "REJECTED", op_type
            assert result["error"]["code"] == "VALIDATION"

    def test_PAY8_a_payment_cannot_be_synchronised_and_leaves_no_trace(
        self, client, db, tenant_a, customer
    ):
        """A-PAY-8: payments are online-only in V1.

        The corrected PAY-8. A device has no way to queue money movement, and the
        refusal is not merely a missing route — the operation is refused with no
        payment, no ledger entry and no commission row anywhere behind it.
        """
        for op_type, payload in (
            (
                "payment.record",
                {"customer_id": customer["id"], "amount_minor": 50000, "method": "CASH"},
            ),
            ("payment.void", {"payment_id": str(uuid7()), "reason": "mistake"}),
        ):
            (result,) = push(
                client,
                tenant_a,
                {"operation_id": str(uuid7()), "op_type": op_type, "payload": payload},
            )
            assert result["status"] == "REJECTED", op_type
            assert result["error"]["code"] == "VALIDATION"
            assert "entity" not in result

        for model in (Payment, LedgerEntry, CommissionEvent):
            assert (
                db.execute(select(func.count()).select_from(model)).scalar_one() == 0
            ), model.__name__
        # And the register is untouched: an unsupported op type burns no id.
        assert (
            db.execute(
                select(func.count())
                .select_from(SyncOperation)
                .where(SyncOperation.op_type.in_(["payment.record", "payment.void"]))
            ).scalar_one()
            == 0
        )

    def test_op_type_and_kind_must_agree(self, client, tenant_a, customer):
        (result,) = push(
            client,
            tenant_a,
            {
                "operation_id": str(uuid7()),
                "op_type": "service.record",
                "payload": {"customer_id": customer["id"], "kind": "SKIP"},
            },
        )
        assert result["status"] == "REJECTED"
        assert result["error"]["code"] == "VALIDATION"


class TestBatchIndependence:
    def test_one_bad_operation_does_not_roll_back_the_others(
        self, client, db, tenant_a, customer, other_customer
    ):
        results = push(
            client,
            tenant_a,
            envelope(customer["id"], quantity="2"),
            envelope(str(uuid7()), quantity="1"),  # unknown customer
            envelope(other_customer["id"], kind="SKIP"),
        )
        assert [r["status"] for r in results] == ["APPLIED", "REJECTED", "APPLIED"]
        assert _records(db, tenant_a) == 2

    def test_a_conflict_mid_batch_leaves_later_entries_applied(
        self, client, db, tenant_a, customer, other_customer
    ):
        push(client, tenant_a, envelope(customer["id"], quantity="2"))
        results = push(
            client,
            tenant_a,
            envelope(customer["id"], quantity="9"),  # conflicts
            envelope(other_customer["id"], quantity="3"),
        )
        assert [r["status"] for r in results] == ["CONFLICT", "APPLIED"]
        assert _records(db, tenant_a) == 2

    def test_results_are_returned_in_request_order_with_matching_ids(
        self, client, tenant_a, customer, other_customer
    ):
        a = envelope(customer["id"], quantity="1")
        b = envelope(other_customer["id"], quantity="1")
        results = push(client, tenant_a, a, b)
        assert [r["operation_id"] for r in results] == [
            a["operation_id"],
            b["operation_id"],
        ]

    def test_empty_batch_is_refused(self, client, tenant_a):
        resp = client.post(SYNC_URL, json={"operations": []}, headers=tenant_a.auth)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION"

    def test_unauthenticated_push_is_401(self, client, customer):
        resp = client.post(SYNC_URL, json={"operations": []})
        assert resp.status_code == 401


class TestBusinessDateIsPreserved:
    def test_an_explicit_past_service_date_is_honoured_not_retargeted(
        self, client, db, tenant_a, customer
    ):
        """The queued intent keeps its business date however late it syncs.

        The date a device sends is the one the *server* last reported to it, and
        the server applies its ordinary rule (not in the future). An entry made
        on Saturday and synchronised on Sunday stays Saturday's.
        """
        (result,) = push(
            client,
            tenant_a,
            envelope(customer["id"], quantity="2", service_date="2026-03-14"),
        )
        assert result["status"] == "APPLIED"
        assert result["entity"]["service_date"] == "2026-03-14"
        record = db.execute(select(DailyServiceRecord)).scalar_one()
        assert record.service_date.isoformat() == "2026-03-14"

    def test_the_same_customer_on_two_days_is_two_records(
        self, client, db, tenant_a, customer
    ):
        results = push(
            client,
            tenant_a,
            envelope(customer["id"], quantity="1", service_date="2026-03-14"),
            envelope(customer["id"], quantity="2", service_date="2026-03-15"),
        )
        assert [r["status"] for r in results] == ["APPLIED", "APPLIED"]
        assert _records(db, tenant_a) == 2

    def test_omitting_service_date_uses_the_tenant_business_date(
        self, client, tenant_a, customer
    ):
        (result,) = push(client, tenant_a, envelope(customer["id"], quantity="1"))
        assert result["entity"]["service_date"] == "2026-03-15"

    def test_client_created_at_does_not_choose_the_date(self, client, tenant_a, customer):
        """R4: a device clock is advisory metadata and nothing more."""
        env = envelope(customer["id"], quantity="1")
        env["client_created_at"] = "2020-01-01T00:00:00Z"
        (result,) = push(client, tenant_a, env)
        assert result["entity"]["service_date"] == "2026-03-15"
