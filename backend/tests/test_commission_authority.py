"""Commission authority over HTTP — COM-7, COM-8, SEC-5, SEC-6, and idempotency.

A-COM-7/8 in full: an owner-admin token is refused on every commission route
including read, no commission field is reachable through any tenant endpoint, and
only the platform principal may create a plan or record a settlement.
"""

from __future__ import annotations

import json

import pytest

from app.core.ids import uuid7
from app.identity.capabilities import (
    ALL_CAPABILITIES,
    CAPABILITIES,
    PLATFORM_CAPABILITIES,
    TENANT_CAPABILITIES,
)
from app.identity.models import Role

pytestmark = pytest.mark.postgres

PRICE = 25000
COMMISSION_FIELDS = (
    "commission",
    "rate_bp",
    "basis_snapshot",
    "fixed_amount_minor",
    "settled_minor",
    "earned_minor",
)


@pytest.fixture
def platform_auth(platform_token):
    return {"Authorization": f"Bearer {platform_token}"}


@pytest.fixture
def a_customer(client, tenant_a):
    resp = client.post(
        "/api/v1/customers",
        json={
            "operation_id": str(uuid7()),
            "code": "A-001",
            "name": "Tenant A Customer",
            "area": "G-10",
            "unit_price_minor": PRICE,
            "default_quantity": "1",
        },
        headers=tenant_a.auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity"]


def _plan_body(tenant, **kw):
    body = {
        "operation_id": str(uuid7()),
        "tenant_id": str(tenant.tenant.id),
        "basis": "RECORDED_VALUE",
        "rate_bp": 250,
        "effective_from": "2026-01-01",
    }
    body.update(kw)
    return body


def _settlement_body(tenant, **kw):
    body = {
        "operation_id": str(uuid7()),
        "tenant_id": str(tenant.tenant.id),
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "amount_minor": 1000,
    }
    body.update(kw)
    return body


class TestSEC5CapabilitiesAreDisjoint:
    def test_SEC5_no_tenant_role_holds_a_commission_capability(self):
        commission = {c for c in ALL_CAPABILITIES if c.startswith("commission:")}
        assert commission == {"commission:read", "commission:adjust", "commission:settle"}
        for role in (Role.OWNER_ADMIN, Role.OPERATOR):
            assert CAPABILITIES[role] & commission == set()

    def test_SEC5_the_two_sets_are_still_disjoint_after_P3(self):
        assert TENANT_CAPABILITIES & PLATFORM_CAPABILITIES == set()

    def test_P3_added_no_capability(self):
        """P0 §3.2 froze three commission capabilities; P3 invents no fourth."""
        assert PLATFORM_CAPABILITIES == {
            "commission:read",
            "commission:adjust",
            "commission:settle",
            "tenant:provision",
            "platform_dashboard:read",
        }


class TestCOM7TenantHasNoAccess:
    """COM-7: no read and no write, through any route."""

    def test_owner_admin_is_refused_the_summary(self, client, tenant_a):
        resp = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_owner_admin_is_refused_the_plan_listing(self, client, tenant_a):
        resp = client.get(
            "/api/v1/platform/commission/plans",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 403

    def test_owner_admin_cannot_create_a_plan(self, client, tenant_a):
        resp = client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=tenant_a.auth,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_owner_admin_cannot_record_a_settlement(self, client, tenant_a):
        resp = client.post(
            "/api/v1/platform/commission/settlements",
            json=_settlement_body(tenant_a),
            headers=tenant_a.auth,
        )
        assert resp.status_code == 403

    def test_a_refused_write_creates_nothing(
        self, client, db, tenant_a, platform_auth
    ):
        client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=tenant_a.auth,
        )
        listed = client.get(
            "/api/v1/platform/commission/plans",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        )
        assert listed.json()["items"] == []

    def test_an_unauthenticated_request_is_401(self, client, tenant_a):
        resp = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
        )
        assert resp.status_code == 401


class TestCOM7NoLeakageThroughTenantEndpoints:
    """A-COM-7/8: no commission field is reachable through a tenant endpoint."""

    def test_no_tenant_response_body_mentions_commission(
        self, client, tenant_a, a_customer, platform_auth
    ):
        # Give the tenant real commission data first, so a leak would have
        # something to leak.
        assert (
            client.post(
                "/api/v1/platform/commission/plans",
                json=_plan_body(tenant_a),
                headers=platform_auth,
            ).status_code
            == 201
        )
        record = client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": a_customer["id"],
                "quantity": "4",
            },
            headers=tenant_a.auth,
        )
        assert record.status_code == 201

        reads = [
            "/api/v1/customers",
            f"/api/v1/customers/{a_customer['id']}",
            f"/api/v1/customers/{a_customer['id']}/statements",
            "/api/v1/billing/cycles",
            f"/api/v1/service/day/{record.json()['entity']['service_date']}",
        ]
        for url in reads:
            resp = client.get(url, headers=tenant_a.auth)
            assert resp.status_code == 200, url
            blob = json.dumps(resp.json()).lower()
            for field in COMMISSION_FIELDS:
                assert field not in blob, f"{field!r} leaked through {url}"

        blob = json.dumps(record.json()).lower()
        for field in COMMISSION_FIELDS:
            assert field not in blob

    def test_no_tenant_route_schema_declares_a_commission_field(self, client):
        schema = client.get("/openapi.json").json()
        tenant_paths = {
            path: ops
            for path, ops in schema["paths"].items()
            if path.startswith("/api/v1") and not path.startswith("/api/v1/platform")
        }
        assert tenant_paths, "tenant path enumeration returned nothing"
        blob = json.dumps(tenant_paths).lower()
        for field in COMMISSION_FIELDS:
            assert field not in blob, f"{field!r} declared on a tenant route"

    def test_the_serializers_expose_no_commission_field(self):
        """Belt and braces at the source: the tenant serializers do not know it."""
        import pathlib

        from tests._source import code_only

        for relative in (
            "app/customers/commands.py",
            "app/service/commands.py",
            "app/payments/commands.py",
            "app/billing/statements.py",
            "app/billing/cycles.py",
        ):
            code = code_only(pathlib.Path(relative))
            assert "CommissionEvent" not in code, relative
            assert "commission_minor" not in code, relative


class TestCOM8PlatformAuthority:
    """COM-8: the platform principal, and only it, may write."""

    def test_a_plan_is_created_and_listed(self, client, tenant_a, platform_auth):
        created = client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        assert created.status_code == 201, created.text
        entity = created.json()["entity"]
        assert entity["basis"] == "RECORDED_VALUE"
        assert entity["rate_bp"] == 250
        assert entity["fixed_amount_minor"] is None
        assert entity["currency"] == "PKR"

        listed = client.get(
            "/api/v1/platform/commission/plans",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        )
        assert [p["id"] for p in listed.json()["items"]] == [entity["id"]]

    def test_a_settlement_is_recorded(self, client, tenant_a, platform_auth):
        resp = client.post(
            "/api/v1/platform/commission/settlements",
            json=_settlement_body(tenant_a),
            headers=platform_auth,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["entity"]["amount_minor"] == 1000

    def test_the_summary_reports_the_four_figures(
        self, client, tenant_a, a_customer, platform_auth
    ):
        client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": a_customer["id"],
                "quantity": "4",
            },
            headers=tenant_a.auth,
        )
        client.post(
            "/api/v1/platform/commission/settlements",
            json=_settlement_body(tenant_a, amount_minor=1000),
            headers=platform_auth,
        )

        summary = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        )
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["earned_minor"] == 2500
        assert body["adjustments_minor"] == 0
        assert body["settled_minor"] == 1000
        assert body["outstanding_minor"] == 1500
        assert body["tenant_id"] == str(tenant_a.tenant.id)
        assert body["currency"] == "PKR"

    def test_the_platform_must_name_the_tenant(self, client, platform_auth):
        """There is no implicit "my tenant" for a platform principal."""
        resp = client.get("/api/v1/platform/commission/summary", headers=platform_auth)
        assert resp.status_code == 422

    def test_an_unknown_tenant_is_404(self, client, platform_auth):
        resp = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(uuid7())},
            headers=platform_auth,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_the_platform_sees_each_tenant_separately(
        self, client, tenant_a, tenant_b, platform_auth
    ):
        client.post(
            "/api/v1/platform/commission/settlements",
            json=_settlement_body(tenant_a, amount_minor=700),
            headers=platform_auth,
        )
        for fixture, expected in ((tenant_a, -700), (tenant_b, 0)):
            body = client.get(
                "/api/v1/platform/commission/summary",
                params={"tenant_id": str(fixture.tenant.id)},
                headers=platform_auth,
            ).json()
            assert body["outstanding_minor"] == expected

    def test_an_overlapping_plan_is_a_409(self, client, tenant_a, platform_auth):
        client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        resp = client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "COMMISSION_PLAN_OVERLAP"

    def test_a_per_event_plan_without_a_fixed_amount_is_422(
        self, client, tenant_a, platform_auth
    ):
        resp = client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a, basis="PER_EVENT", rate_bp=None),
            headers=platform_auth,
        )
        assert resp.status_code == 422

    def test_a_rate_above_10000_is_rejected_at_the_boundary(
        self, client, tenant_a, platform_auth
    ):
        resp = client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a, rate_bp=10001),
            headers=platform_auth,
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize("amount", [0, -1, -1000])
    def test_a_non_positive_settlement_is_rejected_at_the_boundary(
        self, client, tenant_a, platform_auth, amount
    ):
        """A settlement is money that moved. A negative one would be a commission
        adjustment with no terms, no event link and no source fact."""
        resp = client.post(
            "/api/v1/platform/commission/settlements",
            json=_settlement_body(tenant_a, amount_minor=amount),
            headers=platform_auth,
        )
        assert resp.status_code == 422
        assert "amount_minor" in resp.json()["error"]["field_errors"]

    def test_an_over_settlement_is_still_accepted_over_http(
        self, client, tenant_a, a_customer, platform_auth
    ):
        """A-COM-6b through the route: the row is positive, the position is not."""
        client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": a_customer["id"],
                "quantity": "4",
            },
            headers=tenant_a.auth,
        )
        settled = client.post(
            "/api/v1/platform/commission/settlements",
            json=_settlement_body(tenant_a, amount_minor=3000),
            headers=platform_auth,
        )
        assert settled.status_code == 201
        assert settled.json()["entity"]["amount_minor"] == 3000

        summary = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        ).json()
        assert summary["earned_minor"] == 2500
        assert summary["settled_minor"] == 3000
        assert summary["outstanding_minor"] == -500


class TestIdempotencyOfPlatformWrites:
    """The existing register, reused — SYN-1/2/14. There is no second mechanism."""

    def test_a_replayed_plan_creation_is_a_duplicate(
        self, client, tenant_a, platform_auth
    ):
        body = _plan_body(tenant_a)
        first = client.post(
            "/api/v1/platform/commission/plans", json=body, headers=platform_auth
        )
        second = client.post(
            "/api/v1/platform/commission/plans", json=body, headers=platform_auth
        )
        assert first.status_code == 201
        assert second.json()["status"] == "DUPLICATE"
        assert second.json()["entity"]["id"] == first.json()["entity"]["id"]

        listed = client.get(
            "/api/v1/platform/commission/plans",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        )
        assert len(listed.json()["items"]) == 1

    def test_a_replayed_settlement_is_not_recorded_twice(
        self, client, tenant_a, platform_auth
    ):
        body = _settlement_body(tenant_a, amount_minor=400)
        client.post(
            "/api/v1/platform/commission/settlements", json=body, headers=platform_auth
        )
        second = client.post(
            "/api/v1/platform/commission/settlements", json=body, headers=platform_auth
        )
        assert second.json()["status"] == "DUPLICATE"

        summary = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        ).json()
        assert summary["settled_minor"] == 400

    def test_SYN14_the_same_key_with_a_different_payload_fails_closed(
        self, client, tenant_a, platform_auth
    ):
        body = _settlement_body(tenant_a, amount_minor=400)
        client.post(
            "/api/v1/platform/commission/settlements", json=body, headers=platform_auth
        )
        resp = client.post(
            "/api/v1/platform/commission/settlements",
            json={**body, "amount_minor": 900},
            headers=platform_auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"

        summary = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        ).json()
        assert summary["settled_minor"] == 400, "the second request was applied"

    def test_the_register_row_is_written_under_the_target_tenant(
        self, client, db, tenant_a, platform_auth
    ):
        from sqlalchemy import select

        from app.sync.models import SyncOperation

        client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        row = db.execute(
            select(SyncOperation).where(
                SyncOperation.op_type == "commission.plan.create"
            )
        ).scalar_one()
        assert row.tenant_id == tenant_a.tenant.id
        assert row.entity_type == "commission_plan"

    def test_a_repeated_source_record_earns_only_once(
        self, client, tenant_a, a_customer, platform_auth
    ):
        """The prompt's case 8, end to end over HTTP."""
        client.post(
            "/api/v1/platform/commission/plans",
            json=_plan_body(tenant_a),
            headers=platform_auth,
        )
        body = {
            "operation_id": str(uuid7()),
            "customer_id": a_customer["id"],
            "quantity": "4",
        }
        first = client.post(
            "/api/v1/service/records", json=body, headers=tenant_a.auth
        )
        second = client.post(
            "/api/v1/service/records", json=body, headers=tenant_a.auth
        )
        assert first.status_code == 201
        assert second.json()["status"] == "DUPLICATE"

        summary = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers=platform_auth,
        ).json()
        assert summary["earned_minor"] == 2500
