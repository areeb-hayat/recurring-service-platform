"""Manual payments: record, void, and the duplicate-protection rules.

Covers PAY-1..PAY-9, FIN-10, FIN-13, and the AUD guarantees a void must satisfy.
No test here configures, mocks, or imports a payment provider — there is none,
which is FIN-13's whole point.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.audit.models import AuditAction, AuditEvent
from app.billing.cycles import open_cycle
from app.billing.ledger import outstanding_minor
from app.billing.models import EntryKind, LedgerEntry, SourceType
from app.billing.reporting import PaymentState, customer_payment_status
from app.core.errors import IdempotencyKeyReuseError, NotFoundError, ValidationFailed
from app.core.ids import uuid7
from app.payments.commands import (
    RecordPaymentInput,
    VoidPaymentInput,
    record_payment,
    void_payment,
)
from app.payments.models import Payment, PaymentMethod, PaymentStatus
from tests._ops import do_pay, do_record, do_void_payment, entries

pytestmark = pytest.mark.postgres

PRICE = 25000


def _payments(db, ctx, customer):
    return list(
        db.execute(
            select(Payment).where(
                Payment.tenant_id == ctx.tenant_id, Payment.customer_id == customer.id
            )
        )
        .scalars()
        .all()
    )


class TestPAY2Methods:
    @pytest.mark.parametrize("method", ["CASH", "BANK_TRANSFER", "OTHER"])
    def test_PAY2_every_manual_method_is_accepted(
        self, db, tenant_a, customer_factory, method
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = do_pay(db, tenant_a.ctx, customer, 5000, method=method)
        assert outcome.result["method"] == method
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == -5000

    def test_PAY2_an_unknown_method_is_refused_by_the_domain(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(ValidationFailed):
            record_payment(
                db,
                tenant_a.ctx,
                RecordPaymentInput(
                    customer_id=customer.id, amount_minor=100, method="CARD"
                ),
                operation_id=uuid7(),
            )

    def test_PAY2_an_unknown_method_is_refused_by_the_database(
        self, db, tenant_a, customer_factory
    ):
        """A-PAY-2: rejected by the database, not merely by the API layer."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO payment
                      (id, tenant_id, customer_id, amount_minor, method, received_on,
                       status, operation_id, recorded_by_user_id, source, recorded_at)
                    VALUES (gen_random_uuid(), :t, :c, 500, 'STRIPE', CURRENT_DATE,
                            'RECORDED', gen_random_uuid(), :u, 'ONLINE', now())
                    """
                ),
                {
                    "t": str(tenant_a.ctx.tenant_id),
                    "c": str(customer.id),
                    "u": str(tenant_a.owner.id),
                },
            )
        assert "ck_payment_method_valid" in str(exc.value)
        db.rollback()


class TestPAY3PositiveAmount:
    @pytest.mark.parametrize("amount", [0, -100])
    def test_PAY3_non_positive_amount_is_refused_by_the_database(
        self, db, tenant_a, customer_factory, amount
    ):
        """A-PAY-3: direct SQL insert, bypassing the application entirely."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO payment
                      (id, tenant_id, customer_id, amount_minor, method, received_on,
                       status, operation_id, recorded_by_user_id, source, recorded_at)
                    VALUES (gen_random_uuid(), :t, :c, :amount, 'CASH', CURRENT_DATE,
                            'RECORDED', gen_random_uuid(), :u, 'ONLINE', now())
                    """
                ),
                {
                    "t": str(tenant_a.ctx.tenant_id),
                    "c": str(customer.id),
                    "u": str(tenant_a.owner.id),
                    "amount": amount,
                },
            )
        assert "ck_payment_amount_positive" in str(exc.value)
        db.rollback()

    @pytest.mark.parametrize("amount", [0, -100])
    def test_PAY3_non_positive_amount_is_refused_by_the_domain(
        self, db, tenant_a, customer_factory, amount
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(ValidationFailed):
            record_payment(
                db,
                tenant_a.ctx,
                RecordPaymentInput(customer_id=customer.id, amount_minor=amount),
                operation_id=uuid7(),
            )

    def test_FIN1_a_float_amount_is_refused(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(ValidationFailed):
            record_payment(
                db,
                tenant_a.ctx,
                RecordPaymentInput(customer_id=customer.id, amount_minor=100.5),
                operation_id=uuid7(),
            )

    def test_the_api_rejects_a_non_positive_amount(
        self, client, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        db.commit()
        resp = client.post(
            "/api/v1/payments",
            json={
                "operation_id": str(uuid7()),
                "customer_id": str(customer.id),
                "amount_minor": 0,
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION"


class TestPAY1LedgerPosting:
    def test_PAY1_a_payment_posts_exactly_one_negative_entry(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("4"))  # 100000
        outcome = do_pay(db, tenant_a.ctx, customer, 40000)

        payment_entries = [
            e for e in entries(db, tenant_a.ctx, customer.id)
            if e.entry_kind == EntryKind.PAYMENT
        ]
        assert len(payment_entries) == 1
        entry = payment_entries[0]
        assert entry.amount_minor == -40000
        assert entry.source_type == SourceType.PAYMENT
        assert str(entry.source_id) == outcome.result["id"]
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 60000

    def test_the_payment_entry_posts_to_the_open_cycle_on_its_received_date(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        received = tenant_a.ctx.today - timedelta(days=3)
        do_pay(db, tenant_a.ctx, customer, 7000, received_on=received)
        [entry] = entries(db, tenant_a.ctx, customer.id)
        assert entry.occurred_on == received
        assert entry.posting_cycle_id == open_cycle(db, tenant_a.ctx).id

    def test_a_future_received_date_is_refused(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(ValidationFailed) as exc:
            record_payment(
                db,
                tenant_a.ctx,
                RecordPaymentInput(
                    customer_id=customer.id,
                    amount_minor=100,
                    received_on=tenant_a.ctx.today + timedelta(days=1),
                ),
                operation_id=uuid7(),
            )
        assert "received_on" in str(exc.value)

    def test_PAY4_a_foreign_customer_is_404(self, db, tenant_a, tenant_b, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        db.commit()
        with pytest.raises(NotFoundError):
            record_payment(
                db,
                tenant_b.ctx,
                RecordPaymentInput(customer_id=customer.id, amount_minor=100),
                operation_id=uuid7(),
            )


class TestPAY5And6Duplicates:
    def test_PAY5_replaying_an_operation_id_creates_nothing(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()
        first = do_pay(db, tenant_a.ctx, customer, 5000, operation_id=op)
        replay = do_pay(db, tenant_a.ctx, customer, 5000, operation_id=op)
        assert first.status == "APPLIED"
        assert replay.status == "DUPLICATE"
        assert replay.result == first.result
        assert len(_payments(db, tenant_a.ctx, customer)) == 1
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == -5000

    def test_PAY5_five_concurrent_identical_envelopes_post_once(
        self, session_factory, tenant_a, customer_factory, db
    ):
        """A-PAY-5: one payment, one ledger entry, four DUPLICATE responses."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        db.commit()
        op = uuid7()
        results: list = []
        errors: list = []

        def worker():
            session = session_factory()
            try:
                results.append(
                    do_pay(session, tenant_a.ctx, customer, 5000, operation_id=op).status
                )
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], errors
        assert sorted(results) == ["APPLIED", "DUPLICATE", "DUPLICATE", "DUPLICATE", "DUPLICATE"]
        db.expire_all()
        assert len(_payments(db, tenant_a.ctx, customer)) == 1
        assert (
            db.execute(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.entry_kind == EntryKind.PAYMENT)
            ).scalar_one()
            == 1
        )

    def test_PAY6_two_equal_payments_on_the_same_day_both_post(
        self, db, tenant_a, customer_factory
    ):
        """A-PAY-6. This test must fail if anyone adds natural-key deduplication:
        two genuine equal cash payments on one day are legal."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("4"))  # 100000
        do_pay(db, tenant_a.ctx, customer, 50000)
        do_pay(db, tenant_a.ctx, customer, 50000)

        assert len(_payments(db, tenant_a.ctx, customer)) == 2
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0

    def test_SYN14_the_same_operation_id_with_a_different_payload_fails_closed(
        self, db, tenant_a, customer_factory
    ):
        from app.sync.idempotency import execute_idempotent

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        op = uuid7()

        def post(amount):
            return execute_idempotent(
                db,
                tenant_a.ctx,
                operation_id=op,
                op_type="payment.record",
                payload={"customer_id": str(customer.id), "amount_minor": amount},
                perform=lambda: record_payment(
                    db,
                    tenant_a.ctx,
                    RecordPaymentInput(customer_id=customer.id, amount_minor=amount),
                    operation_id=op,
                ),
            )

        post(5000)
        with pytest.raises(IdempotencyKeyReuseError):
            post(9000)
        assert len(_payments(db, tenant_a.ctx, customer)) == 1


class TestFIN10FullPartialAndOverpayment:
    def test_FIN10_partial_then_full_then_overpayment(
        self, db, tenant_a, customer_factory
    ):
        """A-FIN-10 exactly: 1000 billed, 400 paid, 600 paid, 100 more."""
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 1000
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.UNPAID

        do_pay(db, tenant_a.ctx, customer, 400)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 600
        assert (
            customer_payment_status(db, tenant_a.ctx, customer.id)
            == PaymentState.PARTIALLY_PAID
        )

        do_pay(db, tenant_a.ctx, customer, 600)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.PAID

        do_pay(db, tenant_a.ctx, customer, 100)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == -100
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.PAID

    def test_FIN10_an_overpayment_is_never_clamped(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        outcome = do_pay(db, tenant_a.ctx, customer, 5000)
        assert outcome.result["amount_minor"] == 5000
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == -4000


class TestPAY7Void:
    def test_PAY7_void_restores_outstanding_and_keeps_the_row(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 500

        voided = do_void_payment(
            db, tenant_a.ctx, payment.result["id"], reason="bounced cheque"
        )
        assert voided.result["status"] == PaymentStatus.VOIDED
        assert voided.result["voided_reason"] == "bounced cheque"
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 1000

        rows = _payments(db, tenant_a.ctx, customer)
        assert len(rows) == 1
        assert rows[0].status == PaymentStatus.VOIDED
        assert rows[0].amount_minor == 500  # never edited
        assert rows[0].voided_by_user_id == tenant_a.owner.id
        assert rows[0].voided_at == tenant_a.ctx.now

    def test_PAY7_the_reversal_is_a_payment_origin_adjustment(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="entered twice")

        adjustments = [
            e for e in entries(db, tenant_a.ctx, customer.id)
            if e.entry_kind == EntryKind.ADJUSTMENT
        ]
        assert len(adjustments) == 1
        assert adjustments[0].amount_minor == 500
        # Origin, not sign, is what keeps this out of business generated (FIN-14).
        assert adjustments[0].source_type == SourceType.PAYMENT
        assert str(adjustments[0].source_id) == payment.result["id"]

    def test_the_reversal_keeps_the_original_received_date(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        received = tenant_a.ctx.today - timedelta(days=5)
        payment = do_pay(db, tenant_a.ctx, customer, 500, received_on=received)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="error")
        adjustment = [
            e for e in entries(db, tenant_a.ctx, customer.id)
            if e.entry_kind == EntryKind.ADJUSTMENT
        ][0]
        assert adjustment.occurred_on == received

    def test_AUD6_a_void_without_a_reason_is_refused(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        for reason in (None, "", "   "):
            with pytest.raises(ValidationFailed):
                void_payment(
                    db,
                    tenant_a.ctx,
                    payment.result["id"],
                    VoidPaymentInput(reason=reason),
                    operation_id=uuid7(),
                )

    def test_AUD2_a_payment_can_only_be_voided_once(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="first")
        with pytest.raises(ValidationFailed):
            void_payment(
                db,
                tenant_a.ctx,
                payment.result["id"],
                VoidPaymentInput(reason="second"),
                operation_id=uuid7(),
            )

    def test_AUD3_the_void_is_audited_with_before_after_reason_and_actor(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="duplicate entry")

        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.PAYMENT_VOIDED)
        ).scalar_one()
        assert event.before["status"] == "RECORDED"
        assert event.after["status"] == "VOIDED"
        assert event.before["amount_minor"] == 500
        assert event.reason == "duplicate entry"
        assert event.actor_user_id == tenant_a.owner.id
        assert event.occurred_at is not None

    def test_recording_is_audited_too(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_pay(db, tenant_a.ctx, customer, 500, method=PaymentMethod.BANK_TRANSFER)
        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.PAYMENT_RECORDED)
        ).scalar_one()
        assert event.before is None
        assert event.after["method"] == "BANK_TRANSFER"
        assert event.after["amount_minor"] == 500

    def test_a_voided_payment_leaves_the_customer_unpaid_again(
        self, db, tenant_a, customer_factory
    ):
        """Status is derived net of reversals: money that was reversed was never
        collected, so PARTIALLY_PAID must not persist on the strength of it."""
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        payment = do_pay(db, tenant_a.ctx, customer, 400)
        assert (
            customer_payment_status(db, tenant_a.ctx, customer.id)
            == PaymentState.PARTIALLY_PAID
        )
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="reversed")
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.UNPAID


class TestPAY1NoProviderExists:
    """FIN-13 / A-PAY-1: the engine is complete with no gateway anywhere."""

    def test_the_payment_table_has_no_provider_column(self, engine):
        rows = engine.connect().execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='payment'"
            )
        ).fetchall()
        columns = {r[0].lower() for r in rows}
        for forbidden in (
            "provider",
            "provider_reference",
            "provider_message_id",
            "gateway",
            "intent_id",
            "callback_url",
            "verified_at",
        ):
            assert forbidden not in columns

    def test_settings_carry_no_payment_provider_configuration(self):
        from app.core.config import Settings

        fields = set(Settings.model_fields)
        assert not [f for f in fields if "payment" in f or "gateway" in f]

    def test_FIN13_cash_is_recorded_with_no_provider_configured(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        do_pay(db, tenant_a.ctx, customer, 1000, method=PaymentMethod.CASH)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0


class TestPaymentsOverHttp:
    def test_record_and_void_through_the_api(self, client, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        db.commit()
        created = client.post(
            "/api/v1/payments",
            json={
                "operation_id": str(uuid7()),
                "customer_id": str(customer.id),
                "amount_minor": 750,
                "method": "BANK_TRANSFER",
                "reference": "TRX-1",
            },
            headers=tenant_a.auth,
        )
        assert created.status_code == 201, created.text
        entity = created.json()["entity"]
        assert entity["amount_minor"] == 750
        assert entity["status"] == "RECORDED"
        assert entity["currency"] == "PKR"
        # The offline snapshot pages payment history on this (P0 7.1, 7.4).
        assert entity["row_version"] > 0

        voided = client.post(
            f"/api/v1/payments/{entity['id']}/void",
            json={"operation_id": str(uuid7()), "reason": "wrong customer"},
            headers=tenant_a.auth,
        )
        assert voided.status_code == 200, voided.text
        assert voided.json()["entity"]["status"] == "VOIDED"
        assert voided.json()["entity"]["row_version"] > entity["row_version"]

    def test_a_void_without_a_reason_is_a_422(self, client, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        db.commit()
        created = client.post(
            "/api/v1/payments",
            json={
                "operation_id": str(uuid7()),
                "customer_id": str(customer.id),
                "amount_minor": 750,
            },
            headers=tenant_a.auth,
        )
        resp = client.post(
            f"/api/v1/payments/{created.json()['entity']['id']}/void",
            json={"operation_id": str(uuid7())},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 422

    def test_the_customer_read_exposes_derived_status(
        self, client, db, tenant_a, customer_factory
    ):
        """FIN-11: status and outstanding are computed per read, never stored."""
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        db.commit()
        body = client.get(f"/api/v1/customers/{customer.id}", headers=tenant_a.auth).json()
        assert body["outstanding_minor"] == 1000
        assert body["payment_status"] == PaymentState.UNPAID

    def test_FIN11_no_stored_status_column_exists(self, engine):
        rows = engine.connect().execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='customer'"
            )
        ).fetchall()
        columns = {r[0].lower() for r in rows}
        assert "payment_status" not in columns
        assert "outstanding_minor" not in columns
        assert "balance_minor" not in columns


class TestPAY8SyncTransport:
    """PAY-8: the sync transport gets no privileged path and no relaxed rules.

    The offline half of A-PAY-8 — record, restart the browser, sync — needs the
    PWA outbox, which is a later package. What is testable here is that a payment
    arriving as SYNC takes the identical validation path, and that is asserted
    rather than assumed.
    """

    def test_a_synced_payment_is_validated_identically(
        self, db, tenant_a, customer_factory
    ):
        from app.service.models import Source

        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        with pytest.raises(ValidationFailed):
            record_payment(
                db,
                tenant_a.ctx,
                RecordPaymentInput(
                    customer_id=customer.id, amount_minor=0, source=Source.SYNC
                ),
                operation_id=uuid7(),
            )
        outcome = do_pay(db, tenant_a.ctx, customer, 500, source=Source.SYNC)
        assert outcome.result["source"] == "SYNC"

    def test_the_audit_row_records_the_transport(self, db, tenant_a, customer_factory):
        from app.service.models import Source

        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_pay(db, tenant_a.ctx, customer, 500, source=Source.SYNC)
        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.PAYMENT_RECORDED)
        ).scalar_one()
        assert event.source == "SYNC"  # AUD-9
