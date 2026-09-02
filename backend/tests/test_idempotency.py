"""Server-side idempotency register — SYN-1, SYN-2, SYN-3, SYN-13, SYN-14.

This is the mechanism P5's bulk sync endpoint will reuse, so it is tested at the
domain level rather than only through HTTP.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyKeyReuseError, ServiceAlreadyRecordedError
from app.core.ids import uuid7
from app.service.commands import RecordServiceInput, record_service
from app.service.models import DailyServiceRecord
from app.sync.idempotency import compute_request_hash, execute_idempotent
from app.sync.models import SyncOperation

pytestmark = pytest.mark.postgres

PRICE = 25000


def _apply(db, ctx, customer, operation_id, quantity="2", payload=None):
    data = RecordServiceInput(customer_id=customer.id, quantity=Decimal(quantity))
    return execute_idempotent(
        db,
        ctx,
        operation_id=operation_id,
        op_type="service.record",
        payload=payload
        if payload is not None
        else {"customer_id": str(customer.id), "quantity": quantity},
        perform=lambda: record_service(db, ctx, data, operation_id=operation_id),
    )


class TestSYN2Replay:
    def test_SYN2_replay_creates_nothing_and_returns_same_result(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        first = _apply(db, tenant_a.ctx, customer, op)
        second = _apply(db, tenant_a.ctx, customer, op)

        assert first.status == "APPLIED"
        assert second.status == "DUPLICATE"
        # Semantic equality, not byte equality (SYN-2).
        assert second.result == first.result
        assert second.entity_id == first.entity_id

        rows = db.execute(
            select(func.count()).select_from(DailyServiceRecord)
        ).scalar_one()
        assert rows == 1

    def test_SYN2_replay_fires_no_side_effect(self, db, tenant_a, customer_factory):
        from app.billing.ledger import outstanding_minor

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        _apply(db, tenant_a.ctx, customer, op)
        before = outstanding_minor(db, tenant_a.ctx, customer.id)
        for _ in range(4):
            _apply(db, tenant_a.ctx, customer, op)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == before

    def test_SYN2_replay_after_the_active_slot_would_conflict(
        self, db, tenant_a, customer_factory
    ):
        """A retry must replay, not hit the duplicate-service conflict.

        This is the lost-response case: the record exists, so a naive retry would
        collide with the active-day index. The register short-circuits first.
        """
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        _apply(db, tenant_a.ctx, customer, op)
        replay = _apply(db, tenant_a.ctx, customer, op)
        assert replay.status == "DUPLICATE"


class TestSYN3Atomicity:
    def test_SYN3_register_and_effect_commit_together(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        _apply(db, tenant_a.ctx, customer, op)
        records = db.execute(select(func.count()).select_from(DailyServiceRecord)).scalar_one()
        register = db.execute(select(func.count()).select_from(SyncOperation)).scalar_one()
        assert records == 1 and register == 1

    def test_SYN3_failed_operation_leaves_neither(self, db, tenant_a, customer_factory):
        """A validation failure inside perform() must not leave a register row."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()

        def boom():
            raise RuntimeError("simulated failure inside the effect")

        with pytest.raises(RuntimeError):
            execute_idempotent(
                db,
                tenant_a.ctx,
                operation_id=op,
                op_type="service.record",
                payload={"x": 1},
                perform=boom,
            )
        db.rollback()
        assert db.execute(select(func.count()).select_from(SyncOperation)).scalar_one() == 0
        assert (
            db.execute(select(func.count()).select_from(DailyServiceRecord)).scalar_one() == 0
        )
        # And the operation_id is still usable afterwards.
        retry = _apply(db, tenant_a.ctx, customer, op)
        assert retry.status == "APPLIED"


class TestSYN14KeyReuse:
    """SYN-14: an operation_id is bound to the request that created it."""

    def test_SYN14_same_key_different_payload_is_refused(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        _apply(db, tenant_a.ctx, customer, op, quantity="2")
        with pytest.raises(IdempotencyKeyReuseError):
            _apply(db, tenant_a.ctx, customer, op, quantity="9")

    def test_SYN14_refusal_does_not_apply_the_new_request(
        self, db, tenant_a, customer_factory
    ):
        from app.billing.ledger import outstanding_minor

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        _apply(db, tenant_a.ctx, customer, op, quantity="2")
        with pytest.raises(IdempotencyKeyReuseError):
            _apply(db, tenant_a.ctx, customer, op, quantity="9")
        db.rollback()
        # Fails closed: neither the old result silently returned, nor the new applied.
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 2 * PRICE
        assert (
            db.execute(select(func.count()).select_from(DailyServiceRecord)).scalar_one() == 1
        )

    def test_SYN14_error_carries_a_stable_code(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        _apply(db, tenant_a.ctx, customer, op, quantity="2")
        with pytest.raises(IdempotencyKeyReuseError) as exc:
            _apply(db, tenant_a.ctx, customer, op, quantity="3")
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSE"
        assert exc.value.status_code == 409


class TestRequestHash:
    def test_hash_is_stable_across_key_order(self):
        a = compute_request_hash("op", {"x": 1, "y": 2})
        b = compute_request_hash("op", {"y": 2, "x": 1})
        assert a == b

    def test_hash_distinguishes_payloads(self):
        assert compute_request_hash("op", {"q": "2"}) != compute_request_hash("op", {"q": "3"})

    def test_hash_distinguishes_op_types(self):
        assert compute_request_hash("a", {"q": 1}) != compute_request_hash("b", {"q": 1})


class TestSYN13Retention:
    def test_SYN13_no_pruning_mechanism_exists(self):
        """No TTL, archival or cleanup of the register anywhere in the codebase.

        Scans code with comments and string literals stripped, so the module's
        own "never pruned" docstring is not mistaken for a pruning mechanism.
        """
        from tests._source import code_only, python_files

        offenders = []
        for path in python_files():
            code = code_only(path).lower()
            if "syncoperation" not in code:
                continue
            for marker in ("delete", "truncate", "prune", "purge", "ttl"):
                if marker in code.split():
                    offenders.append((path.name, marker))
        assert offenders == [], f"register pruning suspected: {offenders}"

    def test_SYN13_old_operation_still_replays(self, db, tenant_a, customer_factory):
        """No retention horizon: an old operation_id stays replay-safe."""
        from sqlalchemy import update
        from datetime import timedelta

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        first = _apply(db, tenant_a.ctx, customer, op)
        # Age the register row far beyond any plausible retention window.
        db.execute(
            update(SyncOperation)
            .where(SyncOperation.operation_id == op)
            .values(received_at=first.result and func.now() - timedelta(days=3650))
        )
        db.commit()
        replay = _apply(db, tenant_a.ctx, customer, op)
        assert replay.status == "DUPLICATE"
        assert replay.result == first.result


class TestConcurrentReplay:
    """A-SYN-1/2: five concurrent identical envelopes -> one row, four DUPLICATE."""

    def test_concurrent_same_operation_id_creates_one_row(
        self, db, session_factory, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        ctx = tenant_a.ctx
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(5)

        def worker() -> None:
            session: Session = session_factory()
            try:
                barrier.wait(timeout=10)
                data = RecordServiceInput(customer_id=customer.id, quantity=Decimal("2"))
                outcome = execute_idempotent(
                    session,
                    ctx,
                    operation_id=op,
                    op_type="service.record",
                    payload={"customer_id": str(customer.id), "quantity": "2"},
                    perform=lambda: record_service(session, ctx, data, operation_id=op),
                )
                results.append(outcome.status)
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # SYN-15: the business uniqueness conflict must NOT leak out. Before the
        # register claimed the key first, the losers collided on the daily-record
        # active-day index and surfaced as CONFLICT instead of DUPLICATE.
        leaked = [e for e in errors if isinstance(e, ServiceAlreadyRecordedError)]
        assert not leaked, f"business uniqueness conflict leaked to the caller: {leaked}"
        assert not errors, f"unexpected errors: {errors}"
        assert sorted(results) == ["APPLIED", "DUPLICATE", "DUPLICATE", "DUPLICATE", "DUPLICATE"]

        verify = session_factory()
        try:
            rows = verify.execute(
                select(func.count()).select_from(DailyServiceRecord)
            ).scalar_one()
            register = verify.execute(
                select(func.count()).select_from(SyncOperation)
            ).scalar_one()
        finally:
            verify.close()
        assert rows == 1
        assert register == 1
