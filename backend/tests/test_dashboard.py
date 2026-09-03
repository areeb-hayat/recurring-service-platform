"""The owner dashboard — server-authoritative totals (P6, P0 §11.1, §15).

The dashboard is the screen most likely to be built wrong, in one specific way:
by adding up rows on the client. So the tests here are less about the numbers
being present than about them being *the same numbers* the rest of the system
already computes — the four §11.1 derivations, separated by adjustment origin,
never derived from one another.

The case that pins it is the payment-void case (A-FIN-14 / A-FIN-16), asserted
here through the dashboard: a voided payment moves outstanding and collections
and leaves business generated exactly where it was.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.billing.dashboard import dashboard_summary, outstanding_customers
from app.core.ids import uuid7
from app.payments.commands import (
    RecordPaymentInput,
    VoidPaymentInput,
    record_payment,
    void_payment,
)
from app.service.commands import RecordServiceInput, record_service

pytestmark = pytest.mark.postgres

PRICE = 25000  # Rs. 250.00


def bill(db, ctx, customer, quantity="2"):
    record_service(
        db,
        ctx,
        RecordServiceInput(customer_id=customer.id, quantity=Decimal(quantity)),
        operation_id=uuid7(),
    )
    db.commit()


def pay(db, ctx, customer, amount_minor):
    result, _, payment_id = record_payment(
        db,
        ctx,
        RecordPaymentInput(customer_id=customer.id, amount_minor=amount_minor),
        operation_id=uuid7(),
    )
    db.commit()
    return payment_id


class TestSummaryFigures:
    def test_an_empty_business_reports_zeros_and_no_open_cycle(self, db, tenant_a):
        summary = dashboard_summary(db, tenant_a.ctx)
        assert summary["outstanding_minor"] == 0
        assert summary["all_time"] == {
            "business_generated_minor": 0,
            "billed_value_minor": 0,
            "collected_minor": 0,
            "outstanding_minor": 0,
        }
        # Null, not a row of zeros: there is no current period to report on.
        assert summary["open_cycle"] is None
        assert summary["current_cycle"] is None
        assert summary["customers"]["total"] == 0

    def test_the_headline_figures_come_from_the_ledger(
        self, db, tenant_a, customer_factory
    ):
        c1 = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        c2 = customer_factory(tenant_a.ctx, code="C2", price_minor=PRICE)
        bill(db, tenant_a.ctx, c1, "2")  # 50000
        bill(db, tenant_a.ctx, c2, "1")  # 25000
        pay(db, tenant_a.ctx, c1, 20000)

        summary = dashboard_summary(db, tenant_a.ctx)
        assert summary["all_time"]["business_generated_minor"] == 75000
        assert summary["all_time"]["collected_minor"] == 20000
        assert summary["all_time"]["outstanding_minor"] == 55000
        # Nothing has been billed: the cycle is still open (FIN-15 vs FIN-14).
        assert summary["all_time"]["billed_value_minor"] == 0
        assert summary["open_cycle"] is not None
        assert summary["current_cycle"]["business_generated_minor"] == 75000

    def test_a_voided_payment_moves_collections_not_business_generated(
        self, db, tenant_a, customer_factory
    ):
        """A-FIN-14 / A-FIN-16, seen from the dashboard.

        This is the defect the whole §11.1 origin rule exists to prevent: a
        payment-origin ADJUSTMENT summed without filtering by origin would show
        the business as having generated 1500 instead of 1000.
        """
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=50000)
        bill(db, tenant_a.ctx, customer, "1")  # 50000 charged
        payment_id = pay(db, tenant_a.ctx, customer, 25000)

        before = dashboard_summary(db, tenant_a.ctx)["all_time"]
        assert (before["business_generated_minor"], before["collected_minor"],
                before["outstanding_minor"]) == (50000, 25000, 25000)

        void_payment(
            db,
            tenant_a.ctx,
            payment_id,
            VoidPaymentInput(reason="entered against the wrong customer"),
            operation_id=uuid7(),
        )
        db.commit()

        after = dashboard_summary(db, tenant_a.ctx)["all_time"]
        assert after["business_generated_minor"] == 50000, "unchanged by a void"
        assert after["collected_minor"] == 0
        assert after["outstanding_minor"] == 50000

    def test_customer_counts_split_active_owing_and_in_credit(
        self, db, tenant_a, customer_factory
    ):
        owing = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        credit = customer_factory(tenant_a.ctx, code="C2", price_minor=PRICE)
        customer_factory(tenant_a.ctx, code="C3", price_minor=PRICE, status="INACTIVE")

        bill(db, tenant_a.ctx, owing, "2")
        bill(db, tenant_a.ctx, credit, "1")
        pay(db, tenant_a.ctx, credit, 30000)  # FIN-10: an overpayment is a credit

        counts = dashboard_summary(db, tenant_a.ctx)["customers"]
        assert counts == {
            "total": 3,
            "active": 2,
            "with_balance_due": 1,
            "in_credit": 1,
        }

    def test_recent_payment_activity_shows_voids_too(
        self, db, tenant_a, customer_factory
    ):
        """AUD-8: a void is exactly the movement an owner needs to see."""
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        bill(db, tenant_a.ctx, customer, "2")
        payment_id = pay(db, tenant_a.ctx, customer, 10000)
        void_payment(
            db, tenant_a.ctx, payment_id,
            VoidPaymentInput(reason="duplicate entry"), operation_id=uuid7(),
        )
        db.commit()

        recent = dashboard_summary(db, tenant_a.ctx)["recent_payments"]
        assert len(recent) == 1
        assert recent[0]["status"] == "VOIDED"
        assert recent[0]["customer_name"] == customer.name
        assert recent[0]["amount_minor"] == 10000

    def test_no_commission_figure_appears_anywhere_in_the_summary(
        self, db, tenant_a, customer_factory
    ):
        """COM-7: the owner's dashboard is not a commission surface."""
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        bill(db, tenant_a.ctx, customer, "2")
        summary = dashboard_summary(db, tenant_a.ctx)
        assert "commission" not in repr(summary).lower()

    def test_no_operating_cost_figure_appears_in_the_revenue_summary(
        self, db, tenant_a, customer_factory
    ):
        """Provider expenses are a separate concept with a separate screen.

        Mixing them into a customer-revenue summary would produce a figure that
        means nothing — and in a currency that may not even be the tenant's.
        """
        summary = dashboard_summary(db, tenant_a.ctx)
        assert "operating_cost" not in repr(summary)
        assert "estimated" not in repr(summary)


class TestOutstandingList:
    def test_customers_are_listed_most_owed_first(
        self, db, tenant_a, customer_factory
    ):
        small = customer_factory(tenant_a.ctx, code="C1", price_minor=10000)
        large = customer_factory(tenant_a.ctx, code="C2", price_minor=90000)
        settled = customer_factory(tenant_a.ctx, code="C3", price_minor=20000)
        bill(db, tenant_a.ctx, small, "1")
        bill(db, tenant_a.ctx, large, "1")
        bill(db, tenant_a.ctx, settled, "1")
        pay(db, tenant_a.ctx, settled, 20000)

        items = outstanding_customers(db, tenant_a.ctx)["items"]
        assert [i["code"] for i in items] == ["C2", "C1"]
        assert [i["outstanding_minor"] for i in items] == [90000, 10000]

    def test_a_credit_balance_is_listed_rather_than_hidden(
        self, db, tenant_a, customer_factory
    ):
        """Money the business is holding is a fact about a customer too, and
        dropping it would make this page disagree with the summary total."""
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=10000)
        bill(db, tenant_a.ctx, customer, "1")
        pay(db, tenant_a.ctx, customer, 15000)

        items = outstanding_customers(db, tenant_a.ctx)["items"]
        assert [i["outstanding_minor"] for i in items] == [-5000]

    def test_the_list_and_the_summary_agree(self, db, tenant_a, customer_factory):
        for i in range(4):
            customer = customer_factory(
                tenant_a.ctx, code=f"C{i}", price_minor=10000 * (i + 1)
            )
            bill(db, tenant_a.ctx, customer, "1")

        summary_total = dashboard_summary(db, tenant_a.ctx)["outstanding_minor"]
        listed = sum(
            i["outstanding_minor"] for i in outstanding_customers(db, tenant_a.ctx)["items"]
        )
        assert summary_total == listed == 100000


class TestOverHttp:
    def test_the_routes_serialize_what_the_domain_computed(
        self, client, tenant_a, customer_factory, db
    ):
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        bill(db, tenant_a.ctx, customer, "2")

        summary = client.get("/api/v1/dashboard/summary", headers=tenant_a.auth)
        assert summary.status_code == 200
        body = summary.json()
        assert body["currency"] == "PKR" and body["currency_exponent"] == 2
        assert body["business_date"] == tenant_a.ctx.today.isoformat()
        assert body["all_time"]["business_generated_minor"] == 50000

        outstanding = client.get("/api/v1/dashboard/outstanding", headers=tenant_a.auth)
        assert outstanding.status_code == 200
        assert outstanding.json()["items"][0]["outstanding_minor"] == 50000

    def test_customer_financial_reads_are_available_to_the_owner(
        self, client, tenant_a, customer_factory, db
    ):
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        bill(db, tenant_a.ctx, customer, "2")
        payment_id = pay(db, tenant_a.ctx, customer, 10000)
        void_payment(
            db, tenant_a.ctx, payment_id,
            VoidPaymentInput(reason="mistake"), operation_id=uuid7(),
        )
        db.commit()

        payments = client.get(
            f"/api/v1/customers/{customer.id}/payments", headers=tenant_a.auth
        ).json()["items"]
        assert len(payments) == 1 and payments[0]["status"] == "VOIDED"
        assert payments[0]["voided_reason"] == "mistake"

        history = client.get(
            f"/api/v1/customers/{customer.id}/history", headers=tenant_a.auth
        ).json()["items"]
        assert len(history) == 1 and history[0]["charge_minor"] == 50000

        all_payments = client.get("/api/v1/payments", headers=tenant_a.auth).json()
        assert [p["id"] for p in all_payments["items"]] == [payments[0]["id"]]
