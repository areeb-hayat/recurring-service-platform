"""Daily service records: recording, skip, price snapshots, corrections, voids.

Covers FIN-3, FIN-4, FIN-6, FIN-7, FIN-12, SYN-4, AUD-1..AUD-8.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.audit.models import AuditEvent
from app.billing.ledger import outstanding_minor
from app.billing.models import EntryKind, LedgerEntry, SourceType
from app.core.errors import (
    NotFoundError,
    ServiceAlreadyRecordedError,
    ValidationFailed,
)
from app.core.ids import uuid7
from app.service.commands import (
    CorrectServiceInput,
    RecordServiceInput,
    VoidServiceInput,
    correct_service,
    list_day,
    record_service,
    void_service,
)
from app.service.models import DailyServiceRecord, InputMethod, RecordStatus, ServiceKind
from app.sync.idempotency import execute_idempotent

pytestmark = pytest.mark.postgres

PRICE = 25000  # Rs. 250.00 in paisa


def _record(db, ctx, customer, **kw):
    """Record through the idempotent path, as the API does."""
    op = kw.pop("operation_id", None) or uuid7()
    data = RecordServiceInput(customer_id=customer.id, **kw)
    return execute_idempotent(
        db,
        ctx,
        operation_id=op,
        op_type="service.record",
        payload={"customer_id": str(customer.id), "n": str(op)},
        perform=lambda: record_service(db, ctx, data, operation_id=op),
    )


def _entries(db, ctx, customer):
    return list(
        db.execute(
            select(LedgerEntry)
            .where(
                LedgerEntry.tenant_id == ctx.tenant_id,
                LedgerEntry.customer_id == customer.id,
            )
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        )
        .scalars()
        .all()
    )


class TestRecording:
    def test_FIN3_records_charge_from_quantity_and_price(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        assert outcome.status == "APPLIED"
        assert outcome.result["charge_minor"] == 75000
        assert outcome.result["quantity"] == "3.000"
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 75000

    def test_FIN3_fractional_quantity(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=12000)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("1.5"))
        assert outcome.result["charge_minor"] == 18000

    def test_FIN4_outstanding_is_ledger_sum(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        for day in range(3):
            _record(
                db,
                tenant_a.ctx,
                customer,
                quantity=Decimal("2"),
                service_date=tenant_a.ctx.today - timedelta(days=day),
            )
        expected = 3 * 50000
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == expected
        total = db.execute(
            select(func.sum(LedgerEntry.amount_minor)).where(
                LedgerEntry.customer_id == customer.id
            )
        ).scalar_one()
        assert int(total) == expected

    def test_service_creates_exactly_one_charge_entry(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        entries = _entries(db, tenant_a.ctx, customer)
        assert len(entries) == 1
        assert entries[0].entry_kind == EntryKind.CHARGE
        assert entries[0].amount_minor == PRICE
        # Service-origin: this is what makes it count as business generated.
        assert entries[0].source_type == SourceType.DAILY_SERVICE_RECORD

    def test_quantity_is_required_for_service(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(ValidationFailed, match="quantity"):
            _record(db, tenant_a.ctx, customer, quantity=None)

    def test_unknown_customer_is_not_found(self, db, tenant_a):
        data = RecordServiceInput(customer_id=uuid7(), quantity=Decimal("1"))
        with pytest.raises(NotFoundError):
            record_service(db, tenant_a.ctx, data, operation_id=uuid7())

    def test_R4_service_date_defaults_to_tenant_business_date(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        assert outcome.result["service_date"] == tenant_a.ctx.today.isoformat()

    def test_R4_future_service_date_is_rejected(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(ValidationFailed, match="future"):
            _record(
                db,
                tenant_a.ctx,
                customer,
                quantity=Decimal("1"),
                service_date=tenant_a.ctx.today + timedelta(days=1),
            )

    @pytest.mark.parametrize("days_ago", [91, 400, 1000])
    def test_R4_old_explicit_service_date_is_accepted(
        self, db, tenant_a, customer_factory, days_ago
    ):
        """No backdate window exists in V1 — including well beyond 90 days."""
        customer = customer_factory(tenant_a.ctx, code=f"OLD{days_ago}", price_minor=PRICE)
        old_date = tenant_a.ctx.today - timedelta(days=days_ago)
        outcome = _record(
            db, tenant_a.ctx, customer, quantity=Decimal("1"), service_date=old_date
        )
        assert outcome.status == "APPLIED"
        assert outcome.result["service_date"] == old_date.isoformat()
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == PRICE

    def test_R4_client_clock_cannot_redefine_today(self, client, tenant_a, clock):
        """The device's opinion of "now" is not consulted anywhere.

        The request carries no date and an advisory client timestamp far from the
        tenant's business date; the server still stamps its own tenant-local today.
        """
        customer = client.post(
            "/api/v1/customers",
            json={
                "operation_id": str(uuid7()),
                "code": "CLOCK-1",
                "name": "Clock Test",
                "unit_price_minor": PRICE,
                "default_quantity": "1",
            },
            headers=tenant_a.auth,
        ).json()["entity"]

        resp = client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": customer["id"],
                "quantity": "1",
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 201, resp.text
        # The server's tenant-local business date, from the injected clock.
        assert resp.json()["entity"]["service_date"] == tenant_a.ctx.today.isoformat()

    def test_R4_business_date_follows_the_tenant_clock_across_midnight(
        self, client, tenant_a, clock
    ):
        """Advancing the server clock past tenant-local midnight moves "today"."""
        customer = client.post(
            "/api/v1/customers",
            json={
                "operation_id": str(uuid7()),
                "code": "CLOCK-2",
                "name": "Midnight",
                "unit_price_minor": PRICE,
                "default_quantity": "1",
            },
            headers=tenant_a.auth,
        ).json()["entity"]

        first = client.post(
            "/api/v1/service/records",
            json={"operation_id": str(uuid7()), "customer_id": customer["id"], "quantity": "1"},
            headers=tenant_a.auth,
        ).json()["entity"]

        # Cross tenant-local midnight (12:00 PKT -> 01:00 PKT next day) and mint a
        # fresh token, since the 60-minute access token legitimately expires.
        clock.advance(hours=13)
        from app.core.security import encode_access_token
        from tests.conftest import TEST_JWT_SECRET

        fresh = encode_access_token(
            secret=TEST_JWT_SECRET,
            user_id=str(tenant_a.owner.id),
            scope="TENANT",
            role=tenant_a.owner.role,
            tenant_id=str(tenant_a.tenant.id),
            issued_at=clock.now_utc(),
            expires_in_minutes=60,
        )
        second = client.post(
            "/api/v1/service/records",
            json={"operation_id": str(uuid7()), "customer_id": customer["id"], "quantity": "1"},
            headers={"Authorization": f"Bearer {fresh}"},
        )
        assert second.status_code == 201, second.text
        # A different business date, so the active-day slot is free again.
        assert second.json()["entity"]["service_date"] != first["service_date"]


class TestFIN7Skip:
    """FIN-7: a SKIP is a real row with zero quantity/charge and NO ledger entry."""

    def test_FIN7_skip_creates_row_with_zeroes(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = _record(db, tenant_a.ctx, customer, kind=ServiceKind.SKIP)
        assert outcome.result["kind"] == "SKIP"
        assert outcome.result["quantity"] == "0.000"
        assert outcome.result["charge_minor"] == 0

    def test_FIN7_skip_creates_no_ledger_entry(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, kind=ServiceKind.SKIP)
        assert _entries(db, tenant_a.ctx, customer) == []
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0

    def test_FIN7_skip_occupies_the_active_day_slot(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, kind=ServiceKind.SKIP)
        with pytest.raises(ServiceAlreadyRecordedError):
            _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))

    def test_FIN7_database_refuses_a_nonzero_skip(self, db, tenant_a, customer_factory):
        """The invariant is enforced by CHECK, not only by application code."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO daily_service_record
                      (id, tenant_id, customer_id, service_date, quantity,
                       unit_price_minor, unit_label, charge_minor, kind, status,
                       recorded_by_user_id, operation_id, source, input_method, recorded_at)
                    VALUES (gen_random_uuid(), :t, :c, CURRENT_DATE, 5, 100, 'unit', 500,
                            'SKIP', 'ACTIVE', :u, gen_random_uuid(), 'ONLINE', 'BUTTON', now())
                    """
                ),
                {"t": str(tenant_a.ctx.tenant_id), "c": str(customer.id), "u": str(tenant_a.owner.id)},
            )
        assert "skip_is_zero" in str(exc.value)
        db.rollback()


class TestSYN4DuplicatePrevention:
    """SYN-4: the partial unique index is the concurrency guarantee."""

    def test_SYN4_second_record_same_day_conflicts(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        with pytest.raises(ServiceAlreadyRecordedError):
            _record(db, tenant_a.ctx, customer, quantity=Decimal("2"))

    def test_SYN4_only_one_row_persists_after_conflict(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        with pytest.raises(ServiceAlreadyRecordedError):
            _record(db, tenant_a.ctx, customer, quantity=Decimal("9"))
        rows = db.execute(
            select(func.count()).select_from(DailyServiceRecord).where(
                DailyServiceRecord.customer_id == customer.id
            )
        ).scalar_one()
        assert rows == 1
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == PRICE

    def test_SYN4_different_days_are_allowed(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        _record(
            db,
            tenant_a.ctx,
            customer,
            quantity=Decimal("1"),
            service_date=tenant_a.ctx.today - timedelta(days=1),
        )
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 2 * PRICE

    def test_SYN4_index_permits_many_non_active_rows_same_day(
        self, db, tenant_a, customer_factory
    ):
        """Corrections leave several SUPERSEDED rows on one day; only ACTIVE is unique."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        record_id = outcome.entity_id
        for qty in ("2", "1"):
            op = uuid7()
            outcome = execute_idempotent(
                db,
                tenant_a.ctx,
                operation_id=op,
                op_type="service.correct",
                payload={"n": str(op)},
                perform=lambda rid=record_id, q=qty, o=op: correct_service(
                    db,
                    tenant_a.ctx,
                    rid,
                    CorrectServiceInput(quantity=Decimal(q), reason="fix"),
                    operation_id=o,
                ),
            )
            record_id = outcome.entity_id
        active = db.execute(
            select(func.count()).select_from(DailyServiceRecord).where(
                DailyServiceRecord.customer_id == customer.id,
                DailyServiceRecord.status == RecordStatus.ACTIVE,
            )
        ).scalar_one()
        total = db.execute(
            select(func.count()).select_from(DailyServiceRecord).where(
                DailyServiceRecord.customer_id == customer.id
            )
        ).scalar_one()
        assert active == 1 and total == 3


class TestFIN6PriceSnapshot:
    """FIN-6: changing the customer price never rewrites recorded history."""

    def test_FIN6_price_change_does_not_alter_history(self, db, tenant_a, customer_factory):
        # A-FIN-6: record 3 units at Rs. 250, then raise the price to Rs. 300.
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        record_id = outcome.entity_id
        assert outcome.result["charge_minor"] == 75000

        customer.unit_price_minor = 30000
        db.commit()

        record = db.get(DailyServiceRecord, record_id)
        db.refresh(record)
        assert record.unit_price_minor == 25000, "snapshot must not follow the customer"
        assert record.charge_minor == 75000
        entries = _entries(db, tenant_a.ctx, customer)
        assert entries[0].amount_minor == 75000
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 75000

    def test_FIN6_unit_label_is_snapshotted(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        assert outcome.result["unit_label"] == tenant_a.tenant.unit_label

    def test_FIN6_correction_reuses_original_price_not_current(
        self, db, tenant_a, customer_factory
    ):
        """A correction fixes what was delivered, not what it cost."""
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        outcome = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        customer.unit_price_minor = 99999
        db.commit()

        op = uuid7()
        corrected = execute_idempotent(
            db,
            tenant_a.ctx,
            operation_id=op,
            op_type="service.correct",
            payload={"n": str(op)},
            perform=lambda: correct_service(
                db,
                tenant_a.ctx,
                outcome.entity_id,
                CorrectServiceInput(quantity=Decimal("2"), reason="miscount"),
                operation_id=op,
            ),
        )
        assert corrected.result["unit_price_minor"] == 25000
        assert corrected.result["charge_minor"] == 50000


class TestCorrections:
    """AUD-2..AUD-6: governed correction with a full, linked history."""

    def _correct(self, db, ctx, record_id, quantity, reason="miscount", **kw):
        op = uuid7()
        return execute_idempotent(
            db,
            ctx,
            operation_id=op,
            op_type="service.correct",
            payload={"n": str(op)},
            perform=lambda: correct_service(
                db,
                ctx,
                record_id,
                CorrectServiceInput(quantity=quantity, reason=reason, **kw),
                operation_id=op,
            ),
        )

    def test_AUD3_AUD5_correction_preserves_original_and_posts_difference(
        self, db, tenant_a, customer_factory
    ):
        # A-AUD-3/5: 3 units at Rs.250 (Rs.750) corrected down to 2 units.
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        second = self._correct(db, tenant_a.ctx, first.entity_id, Decimal("2"))

        original = db.get(DailyServiceRecord, first.entity_id)
        db.refresh(original)
        replacement = db.get(DailyServiceRecord, second.entity_id)

        assert original.status == RecordStatus.SUPERSEDED
        assert original.charge_minor == 75000, "the original value survives"
        assert replacement.charge_minor == 50000
        assert replacement.adjustment_minor == -25000  # AUD-5
        assert replacement.reason == "miscount"  # AUD-3
        assert replacement.recorded_by_user_id == tenant_a.owner.id
        assert replacement.recorded_at is not None

        entries = _entries(db, tenant_a.ctx, customer)
        kinds = [(e.entry_kind, e.amount_minor) for e in entries]
        assert (EntryKind.CHARGE, 75000) in kinds
        assert (EntryKind.ADJUSTMENT, -25000) in kinds
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 50000

    def test_AUD4_chain_is_walkable_in_both_directions(self, db, tenant_a, customer_factory):
        # A-AUD-4: three successive corrections, exactly one ACTIVE at the end.
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        r1 = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        r2 = self._correct(db, tenant_a.ctx, r1.entity_id, Decimal("2"))
        r3 = self._correct(db, tenant_a.ctx, r2.entity_id, Decimal("1"))

        first = db.get(DailyServiceRecord, r1.entity_id)
        second = db.get(DailyServiceRecord, r2.entity_id)
        third = db.get(DailyServiceRecord, r3.entity_id)
        for row in (first, second, third):
            db.refresh(row)

        # forwards
        assert first.superseded_by_id == second.id
        assert second.superseded_by_id == third.id
        assert third.superseded_by_id is None
        # backwards
        assert third.corrects_id == second.id
        assert second.corrects_id == first.id
        assert first.corrects_id is None

        assert [r.status for r in (first, second, third)] == [
            RecordStatus.SUPERSEDED,
            RecordStatus.SUPERSEDED,
            RecordStatus.ACTIVE,
        ]
        # 75000 - 25000 - 25000 = 25000, the final charge.
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 25000
        assert third.charge_minor == 25000

    def test_AUD6_reason_is_mandatory(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        with pytest.raises(ValidationFailed, match="reason"):
            correct_service(
                db,
                tenant_a.ctx,
                first.entity_id,
                CorrectServiceInput(quantity=Decimal("2"), reason="   "),
                operation_id=uuid7(),
            )

    def test_correction_to_same_quantity_posts_no_ledger_entry(
        self, db, tenant_a, customer_factory
    ):
        """A zero adjustment has no financial meaning and must not be posted."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        self._correct(db, tenant_a.ctx, first.entity_id, Decimal("2"), reason="typo in note")
        entries = _entries(db, tenant_a.ctx, customer)
        assert len(entries) == 1  # only the original CHARGE
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 2 * PRICE

    def test_correcting_service_to_skip(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        result = self._correct(
            db, tenant_a.ctx, first.entity_id, None, reason="not delivered", kind=ServiceKind.SKIP
        )
        assert result.result["kind"] == "SKIP"
        assert result.result["charge_minor"] == 0
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0

    def test_cannot_correct_a_superseded_record(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        self._correct(db, tenant_a.ctx, first.entity_id, Decimal("2"))
        with pytest.raises(ValidationFailed, match="ACTIVE"):
            correct_service(
                db,
                tenant_a.ctx,
                first.entity_id,
                CorrectServiceInput(quantity=Decimal("1"), reason="again"),
                operation_id=uuid7(),
            )


class TestVoid:
    def _void(self, db, ctx, record_id, reason="entered twice"):
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

    def test_void_appends_compensating_entry_and_keeps_row(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        self._void(db, tenant_a.ctx, first.entity_id)

        record = db.get(DailyServiceRecord, first.entity_id)
        db.refresh(record)
        assert record.status == RecordStatus.VOIDED
        assert record.reason == "entered twice"
        assert record.charge_minor == 50000, "the original value is not erased"

        entries = _entries(db, tenant_a.ctx, customer)
        assert [(e.entry_kind, e.amount_minor) for e in entries] == [
            (EntryKind.CHARGE, 50000),
            (EntryKind.ADJUSTMENT, -50000),
        ]
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0

    def test_void_after_corrections_returns_outstanding_to_zero(
        self, db, tenant_a, customer_factory
    ):
        """The active record's charge is always the net effect, so one reversal suffices."""
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        r1 = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        op = uuid7()
        r2 = execute_idempotent(
            db,
            tenant_a.ctx,
            operation_id=op,
            op_type="service.correct",
            payload={"n": str(op)},
            perform=lambda: correct_service(
                db,
                tenant_a.ctx,
                r1.entity_id,
                CorrectServiceInput(quantity=Decimal("1"), reason="fix"),
                operation_id=op,
            ),
        )
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 25000
        self._void(db, tenant_a.ctx, r2.entity_id)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0

    def test_AUD6_void_requires_reason(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        with pytest.raises(ValidationFailed, match="reason"):
            void_service(
                db, tenant_a.ctx, first.entity_id, VoidServiceInput(reason=""), operation_id=uuid7()
            )

    def test_void_frees_the_day_slot(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        self._void(db, tenant_a.ctx, first.entity_id)
        again = _record(db, tenant_a.ctx, customer, quantity=Decimal("5"))
        assert again.status == "APPLIED"
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 5 * PRICE

    def test_voiding_a_skip_posts_nothing(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, kind=ServiceKind.SKIP)
        self._void(db, tenant_a.ctx, first.entity_id)
        assert _entries(db, tenant_a.ctx, customer) == []


class TestAUD8HistoryVisible:
    def test_AUD8_history_includes_superseded_and_voided(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        op = uuid7()
        execute_idempotent(
            db,
            tenant_a.ctx,
            operation_id=op,
            op_type="service.correct",
            payload={"n": str(op)},
            perform=lambda: correct_service(
                db,
                tenant_a.ctx,
                first.entity_id,
                CorrectServiceInput(quantity=Decimal("2"), reason="fix"),
                operation_id=op,
            ),
        )
        active_only = list_day(db, tenant_a.ctx, tenant_a.ctx.today)
        with_history = list_day(
            db, tenant_a.ctx, tenant_a.ctx.today, include_history=True
        )
        assert len(active_only) == 1
        assert len(with_history) == 2
        assert {r.status for r in with_history} == {
            RecordStatus.ACTIVE,
            RecordStatus.SUPERSEDED,
        }
        # The sum of ACTIVE rows reconciles with outstanding.
        assert sum(r.charge_minor for r in active_only) == outstanding_minor(
            db, tenant_a.ctx, customer.id
        )


class TestProvenance:
    """VOI-8 (early): input_method is metadata and changes nothing."""

    def test_voice_provenance_is_recorded(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        outcome = _record(
            db,
            tenant_a.ctx,
            customer,
            quantity=Decimal("2"),
            input_method=InputMethod.VOICE,
        )
        assert outcome.result["input_method"] == "VOICE"

    def test_voice_and_button_records_are_otherwise_identical(
        self, db, tenant_a, customer_factory
    ):
        c1 = customer_factory(tenant_a.ctx, code="V1", price_minor=PRICE)
        c2 = customer_factory(tenant_a.ctx, code="B1", price_minor=PRICE)
        voice = _record(
            db, tenant_a.ctx, c1, quantity=Decimal("2"), input_method=InputMethod.VOICE
        )
        button = _record(
            db, tenant_a.ctx, c2, quantity=Decimal("2"), input_method=InputMethod.BUTTON
        )
        ignore = {"id", "customer_id", "operation_id", "recorded_at", "row_version", "input_method"}
        assert {k: v for k, v in voice.result.items() if k not in ignore} == {
            k: v for k, v in button.result.items() if k not in ignore
        }
        assert outstanding_minor(db, tenant_a.ctx, c1.id) == outstanding_minor(
            db, tenant_a.ctx, c2.id
        )


class TestAuditTrail:
    def test_audit_events_written_for_each_mutation(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        op = uuid7()
        execute_idempotent(
            db,
            tenant_a.ctx,
            operation_id=op,
            op_type="service.correct",
            payload={"n": str(op)},
            perform=lambda: correct_service(
                db,
                tenant_a.ctx,
                first.entity_id,
                CorrectServiceInput(quantity=Decimal("2"), reason="fix"),
                operation_id=op,
            ),
        )
        actions = [
            r.action
            for r in db.execute(
                select(AuditEvent).order_by(AuditEvent.occurred_at)
            ).scalars()
        ]
        assert "service.recorded" in actions
        assert "service.corrected" in actions

    def test_AUD3_audit_captures_before_after_actor_reason(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        first = _record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        op = uuid7()
        execute_idempotent(
            db,
            tenant_a.ctx,
            operation_id=op,
            op_type="service.correct",
            payload={"n": str(op)},
            perform=lambda: correct_service(
                db,
                tenant_a.ctx,
                first.entity_id,
                CorrectServiceInput(quantity=Decimal("2"), reason="miscount"),
                operation_id=op,
            ),
        )
        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == "service.corrected")
        ).scalar_one()
        assert event.before["charge_minor"] == 75000
        assert event.after["charge_minor"] == 50000
        assert event.reason == "miscount"
        assert event.actor_user_id == tenant_a.owner.id
        assert event.occurred_at is not None
        assert event.operation_id == op
        assert event.source == "ONLINE"  # AUD-9
