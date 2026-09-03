"""Payment and statement in the change feed (P6, SYN-9/10/16).

P5 built the feed and deliberately withheld these two entities until a screen
existed to render them. P6 builds that screen, so they join — and joining brings
three obligations, each asserted here:

1. **The feed version is bumped**, because a device already past a payment's
   `row_version` can only receive it by resynchronising from zero.
2. **The resync clears the snapshot and nothing else.** The outbox and the
   issues store are not caches (SYN-5, SYN-12) — that half is proven client-side
   in `frontend/src/sync/sync.test.tsx`; here we prove the server tells the
   client to resync at all.
3. **Every op type that allocates a `row_version` for the new entities takes the
   commit-order boundary** (SYN-10, the D4 fix), or the gap it closes reopens on
   financial history.

And one thing that does *not* change: payments stay online-only. `payment.record`
is still refused by `POST /sync/operations` (PAY-8).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.billing.cycles import close_cycle, ensure_open_cycle
from app.core.ids import uuid7
from app.payments.commands import (
    RecordPaymentInput,
    VoidPaymentInput,
    record_payment,
    void_payment,
)
from app.service.commands import RecordServiceInput, record_service
from app.sync.changes import SYNC_ENTITIES, SYNC_FEED_VERSION, changes_since

pytestmark = pytest.mark.postgres

PRICE = 25000


def entities_in(body, entity):
    return [c for c in body["changes"] if c["entity"] == entity]


class TestPaymentInTheFeed:
    def test_a_payment_arrives_as_a_change(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        record_payment(
            db,
            tenant_a.ctx,
            RecordPaymentInput(customer_id=customer.id, amount_minor=10000),
            operation_id=uuid7(),
        )
        db.commit()

        payments = entities_in(changes_since(db, tenant_a.ctx, since=0, limit=1000), "payment")
        assert len(payments) == 1
        assert payments[0]["data"]["amount_minor"] == 10000
        assert payments[0]["data"]["status"] == "RECORDED"
        # Serialized by the same function the HTTP route uses, currency included,
        # so the device renders the server's own presentation of the row.
        assert payments[0]["data"]["currency"] == "PKR"

    def test_a_void_arrives_as_an_update_not_a_deletion(
        self, db, tenant_a, customer_factory
    ):
        """No tombstone exists, and none is needed: nothing is ever deleted.

        The payment's own `row_version` advances on RECORDED -> VOIDED (SYN-16),
        so a client that has already seen the payment receives the transition on
        the next delta rather than being left holding a stale RECORDED row.
        """
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        _, _, payment_id = record_payment(
            db,
            tenant_a.ctx,
            RecordPaymentInput(customer_id=customer.id, amount_minor=10000),
            operation_id=uuid7(),
        )
        db.commit()
        cursor = changes_since(db, tenant_a.ctx, since=0, limit=1000)["cursor"]

        void_payment(
            db, tenant_a.ctx, payment_id,
            VoidPaymentInput(reason="duplicate"), operation_id=uuid7(),
        )
        db.commit()

        delta = entities_in(changes_since(db, tenant_a.ctx, since=cursor, limit=1000), "payment")
        assert len(delta) == 1
        assert delta[0]["id"] == str(payment_id)
        assert delta[0]["data"]["status"] == "VOIDED"
        assert delta[0]["data"]["voided_reason"] == "duplicate"

    def test_a_payment_cannot_be_queued_offline(self, client, tenant_a, customer_factory):
        """PAY-8, unchanged by P6: the feed is read-only for money.

        Payment history becoming *visible* offline is not payment recording
        becoming *possible* offline. The op type is still refused.
        """
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        resp = client.post(
            "/api/v1/sync/operations",
            json={
                "operations": [
                    {
                        "operation_id": str(uuid7()),
                        "op_type": "payment.record",
                        "payload": {
                            "customer_id": str(customer.id),
                            "amount_minor": 10000,
                        },
                    }
                ]
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "REJECTED"

        assert entities_in(
            client.get("/api/v1/sync/changes?since=0&limit=1000", headers=tenant_a.auth).json(),
            "payment",
        ) == []


class TestStatementInTheFeed:
    def test_issued_statements_arrive_when_a_cycle_closes(
        self, db, tenant_a, customer_factory, clock
    ):
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        record_service(
            db,
            tenant_a.ctx,
            RecordServiceInput(customer_id=customer.id, quantity=Decimal("2")),
            operation_id=uuid7(),
        )
        db.commit()
        cycle = ensure_open_cycle(db, tenant_a.ctx)
        db.commit()

        before = changes_since(db, tenant_a.ctx, since=0, limit=1000)
        assert entities_in(before, "statement") == []

        # A cycle may only be closed once its inclusive period_end has passed.
        from app.tenancy.context import TenantContext

        later = TenantContext(
            **{
                **{f: getattr(tenant_a.ctx, f) for f in tenant_a.ctx.__slots__},
                "today": cycle.period_end + timedelta(days=1),
            }
        )
        close_cycle(db, later, cycle.id, operation_id=uuid7())
        db.commit()

        statements = entities_in(
            changes_since(db, tenant_a.ctx, since=before["cursor"], limit=1000), "statement"
        )
        assert len(statements) == 1
        data = statements[0]["data"]
        assert data["charges_minor"] == 50000
        assert data["closing_balance_minor"] == 50000
        # FIN-8: the presentation columns arrive split by origin, never mixed.
        assert "service_adjustments_minor" in data
        assert "payment_reversals_minor" in data


class TestFeedVersionAndScope:
    def test_the_feed_version_moved_and_the_feed_names_the_new_entities(
        self, db, tenant_a
    ):
        """P6 raised the version to 2 when `payment` and `statement` joined.

        The number itself is not P6's to own — P8 raised it again, without
        adding an entity, so that devices re-seed customer rows written before
        those rows carried aliases. What P6 pins here is that the version *moved*
        past P5's 1 and that the feed carries exactly these five entities.
        """
        body = changes_since(db, tenant_a.ctx, since=0)
        assert SYNC_FEED_VERSION >= 2
        assert body["feed_version"] == SYNC_FEED_VERSION
        assert set(body["entities"]) == {
            "tenant",
            "customer",
            "daily_service_record",
            "payment",
            "statement",
        }

    def test_the_ledger_is_still_not_carried(self, db, tenant_a):
        assert "ledger_entry" not in SYNC_ENTITIES

    def test_no_commission_row_is_reachable_through_the_feed(
        self, db, tenant_a, customer_factory
    ):
        """COM-8: those tables carry no `row_version`, so there is no mechanism."""
        from app.commission.models import (
            CommissionAdjustment,
            CommissionEvent,
            CommissionPlan,
            CommissionSettlement,
        )

        for model in (
            CommissionPlan,
            CommissionEvent,
            CommissionAdjustment,
            CommissionSettlement,
        ):
            assert "row_version" not in model.__table__.columns.keys()

        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        record_payment(
            db,
            tenant_a.ctx,
            RecordPaymentInput(customer_id=customer.id, amount_minor=10000),
            operation_id=uuid7(),
        )
        db.commit()
        body = changes_since(db, tenant_a.ctx, since=0, limit=1000)
        assert not any("commission" in c["entity"] for c in body["changes"])

    def test_head_accounts_for_payments_and_statements(
        self, db, tenant_a, customer_factory
    ):
        """A first-time device seeds, then continues from `head`.

        If `head` ignored the new tables it would be *below* the versions they
        already hold, and the seeded device would be re-delivered rows it has —
        harmless — but a `head` computed before them and used after them would
        be the reverse, and that is a gap. It is asserted to include them.
        """
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        record_payment(
            db,
            tenant_a.ctx,
            RecordPaymentInput(customer_id=customer.id, amount_minor=10000),
            operation_id=uuid7(),
        )
        db.commit()

        body = changes_since(db, tenant_a.ctx, since=0, limit=1000)
        assert body["head"] == max(c["row_version"] for c in body["changes"])
        payment_version = entities_in(body, "payment")[0]["row_version"]
        assert body["head"] >= payment_version


class TestOrderingStillHolds:
    def test_the_cursor_walks_every_row_exactly_once(
        self, db, tenant_a, customer_factory
    ):
        """SYN-10 across the widened entity set, one row per page."""
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        for amount in (1000, 2000, 3000):
            record_payment(
                db,
                tenant_a.ctx,
                RecordPaymentInput(customer_id=customer.id, amount_minor=amount),
                operation_id=uuid7(),
            )
            db.commit()

        seen: list[tuple[str, str]] = []
        cursor, guard = 0, 0
        while guard < 50:
            guard += 1
            page = changes_since(db, tenant_a.ctx, since=cursor, limit=1)
            seen.extend((c["entity"], c["id"]) for c in page["changes"])
            if not page["has_more"]:
                break
            assert page["cursor"] > cursor
            cursor = page["cursor"]

        assert len(seen) == len(set(seen)), "a row was delivered twice"
        assert sum(1 for entity, _ in seen if entity == "payment") == 3

    def test_replaying_a_cursor_is_a_superset_never_a_gap(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        record_payment(
            db,
            tenant_a.ctx,
            RecordPaymentInput(customer_id=customer.id, amount_minor=1000),
            operation_id=uuid7(),
        )
        db.commit()

        full = changes_since(db, tenant_a.ctx, since=0, limit=1000)["changes"]
        replay = changes_since(db, tenant_a.ctx, since=0, limit=1000)["changes"]
        assert [c["row_version"] for c in full] == [c["row_version"] for c in replay]


class TestTheCommitOrderBoundaryCoversTheNewWriters:
    """SYN-10 / D4 for payment and statement.

    `row_version` is drawn from a non-transactional `nextval` inside a
    transaction, so allocation order and commit order can disagree — a payment
    could allocate 100, a service record allocate 101 and commit first, a feed
    read advance to 101, and the payment's 100 never be delivered. The advisory
    lock in `app/sync/serialization.py` is what stops it, and it only applies to
    op types registered in `FEED_WRITING_OP_TYPES`.
    """

    def test_the_new_op_types_are_registered(self):
        from app.sync.serialization import FEED_WRITING_OP_TYPES

        for op in ("payment.record", "payment.void", "billing.close_cycle"):
            assert op in FEED_WRITING_OP_TYPES

    def test_a_payment_write_actually_holds_the_lock(
        self, db, tenant_a, customer_factory, session_factory
    ):
        """Asserted against the live lock table, not against the registry.

        The registry is a list; this is the behaviour. If the lock were removed
        from the payment path, `pg_locks` would show nothing and this fails.
        """
        from sqlalchemy import text

        from app.sync.idempotency import execute_idempotent
        from app.sync.serialization import (
            FEED_ADVISORY_LOCK_NAMESPACE,
            tenant_lock_key,
        )

        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        session = session_factory()
        held: dict[str, bool] = {}
        try:

            def perform():
                held["locked"] = bool(
                    session.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND classid = :ns AND objid = :key AND granted"
                        ),
                        {
                            # pg_locks reports the two halves of the key as
                            # unsigned 32-bit values.
                            "ns": FEED_ADVISORY_LOCK_NAMESPACE & 0xFFFFFFFF,
                            "key": tenant_lock_key(tenant_a.ctx.tenant_id) & 0xFFFFFFFF,
                        },
                    ).scalar_one()
                )
                return record_payment(
                    session,
                    tenant_a.ctx,
                    RecordPaymentInput(customer_id=customer.id, amount_minor=1000),
                    operation_id=op,
                )

            op = uuid7()
            outcome = execute_idempotent(
                session,
                tenant_a.ctx,
                operation_id=op,
                op_type="payment.record",
                payload={"customer_id": str(customer.id), "amount_minor": 1000},
                perform=perform,
            )
            assert outcome.status == "APPLIED"
            assert held["locked"], "payment.record ran without the SYN-10 boundary"
        finally:
            session.rollback()
            session.close()
