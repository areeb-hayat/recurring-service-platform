"""Tenant isolation — SEC-1..SEC-6, via HTTP and via direct SQL.

A-SEC-3/4 requires route enumeration from OpenAPI so a newly added scoped route
cannot silently escape the suite. :func:`tenant_scoped_routes` derives the list
from the live app, and :meth:`test_route_inventory_is_covered` fails when a new
route appears that the explicit cases do not exercise.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.service.commands import RecordServiceInput, record_service
from app.sync.idempotency import execute_idempotent

pytestmark = pytest.mark.postgres

PRICE = 25000


def tenant_scoped_routes(app) -> list[tuple[str, str]]:
    """Every non-auth, non-meta API route, taken from the app's own route table."""
    out: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if not methods or not path.startswith("/api/v1"):
            continue
        if path.startswith("/api/v1/auth"):
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return sorted(out)


@pytest.fixture
def a_customer(client, tenant_a):
    body = {
        "operation_id": str(uuid7()),
        "code": "A-001",
        "name": "Tenant A Customer",
        "area": "G-10",
        "unit_price_minor": PRICE,
        "default_quantity": "1",
    }
    resp = client.post("/api/v1/customers", json=body, headers=tenant_a.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["entity"]


@pytest.fixture
def a_record(client, tenant_a, a_customer):
    body = {
        "operation_id": str(uuid7()),
        "customer_id": a_customer["id"],
        "quantity": "2",
    }
    resp = client.post("/api/v1/service/records", json=body, headers=tenant_a.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["entity"]


class TestSEC4CrossTenantIsReported404:
    """SEC-4: a foreign identifier is 404 — never 403, never data."""

    def test_SEC4_cannot_read_other_tenants_customer(
        self, client, tenant_a, tenant_b, a_customer
    ):
        resp = client.get(f"/api/v1/customers/{a_customer['id']}", headers=tenant_b.auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_SEC4_cannot_update_other_tenants_customer(
        self, client, tenant_a, tenant_b, a_customer
    ):
        resp = client.patch(
            f"/api/v1/customers/{a_customer['id']}",
            json={"operation_id": str(uuid7()), "name": "Hijacked"},
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_cannot_record_service_against_other_tenants_customer(
        self, client, tenant_a, tenant_b, a_customer
    ):
        resp = client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": a_customer["id"],
                "quantity": "1",
            },
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_cannot_correct_other_tenants_record(
        self, client, tenant_a, tenant_b, a_record
    ):
        resp = client.post(
            f"/api/v1/service/records/{a_record['id']}/correct",
            json={"operation_id": str(uuid7()), "quantity": "9", "reason": "hijack"},
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_cannot_void_other_tenants_record(
        self, client, tenant_a, tenant_b, a_record
    ):
        resp = client.post(
            f"/api/v1/service/records/{a_record['id']}/void",
            json={"operation_id": str(uuid7()), "reason": "hijack"},
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_listing_never_leaks_other_tenants_rows(
        self, client, tenant_a, tenant_b, a_customer
    ):
        resp = client.get("/api/v1/customers", headers=tenant_b.auth)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_SEC4_day_listing_is_scoped(self, client, tenant_a, tenant_b, a_record):
        date = a_record["service_date"]
        assert client.get(f"/api/v1/service/day/{date}", headers=tenant_b.auth).json()[
            "items"
        ] == []
        assert (
            len(client.get(f"/api/v1/service/day/{date}", headers=tenant_a.auth).json()["items"])
            == 1
        )


class TestSEC6ScopeSeparation:
    def test_SEC6_platform_token_rejected_on_tenant_routes(
        self, client, platform_token, a_customer
    ):
        headers = {"Authorization": f"Bearer {platform_token}"}
        resp = client.get("/api/v1/customers", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_SEC6_platform_token_rejected_on_every_tenant_route(
        self, client, app, platform_token, a_customer, a_record
    ):
        headers = {"Authorization": f"Bearer {platform_token}"}
        for method, path in tenant_scoped_routes(app):
            url = (
                path.replace("{customer_id}", a_customer["id"])
                .replace("{record_id}", a_record["id"])
                .replace("{service_date}", a_record["service_date"])
            )
            resp = client.request(method, url, json={"operation_id": str(uuid7())}, headers=headers)
            assert resp.status_code in (403, 422), f"{method} {url} -> {resp.status_code}"
            if resp.status_code == 403:
                assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_SEC6_no_platform_routes_exist_yet(self, app):
        """P1 implements no platform endpoints; nothing to leak in either direction."""
        platform_paths = [
            r.path for r in app.routes if getattr(r, "path", "").startswith("/api/v1/platform")
        ]
        assert platform_paths == []

    def test_unauthenticated_request_is_401(self, client):
        resp = client.get("/api/v1/customers")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


class TestSEC2DatabaseLevelIsolation:
    """SEC-2: the database physically refuses a cross-tenant reference."""

    def test_SEC2_direct_sql_cross_tenant_service_record_is_rejected(
        self, db, tenant_a, tenant_b, customer_factory
    ):
        customer_a = customer_factory(tenant_a.ctx, code="XA", price_minor=PRICE)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO daily_service_record
                      (id, tenant_id, customer_id, service_date, quantity,
                       unit_price_minor, unit_label, charge_minor, kind, status,
                       recorded_by_user_id, operation_id, source, input_method, recorded_at)
                    VALUES (gen_random_uuid(), :tenant_b, :customer_a, CURRENT_DATE, 1,
                            100, 'unit', 100, 'SERVICE', 'ACTIVE', :user_b,
                            gen_random_uuid(), 'ONLINE', 'BUTTON', now())
                    """
                ),
                {
                    "tenant_b": str(tenant_b.ctx.tenant_id),
                    "customer_a": str(customer_a.id),
                    "user_b": str(tenant_b.owner.id),
                },
            )
        assert "fk_daily_service_record_tenant_id_customer_id" in str(exc.value)
        db.rollback()

    def test_SEC2_direct_sql_cross_tenant_ledger_entry_is_rejected(
        self, db, tenant_a, tenant_b, customer_factory
    ):
        customer_a = customer_factory(tenant_a.ctx, code="XB", price_minor=PRICE)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO ledger_entry
                      (id, tenant_id, customer_id, entry_kind, amount_minor,
                       occurred_on, source_type, source_id, created_at)
                    VALUES (gen_random_uuid(), :tenant_b, :customer_a, 'CHARGE', 100,
                            CURRENT_DATE, 'daily_service_record', gen_random_uuid(), now())
                    """
                ),
                {
                    "tenant_b": str(tenant_b.ctx.tenant_id),
                    "customer_a": str(customer_a.id),
                },
            )
        assert "fk_ledger_entry_tenant_id_customer_id" in str(exc.value)
        db.rollback()

    def test_SEC2_domain_layer_refuses_foreign_customer(
        self, db, tenant_a, tenant_b, customer_factory
    ):
        from app.core.errors import NotFoundError

        customer_a = customer_factory(tenant_a.ctx, code="XC", price_minor=PRICE)
        data = RecordServiceInput(customer_id=customer_a.id, quantity=Decimal("1"))
        with pytest.raises(NotFoundError):
            record_service(db, tenant_b.ctx, data, operation_id=uuid7())


class TestSEC3OperationIdIsTenantScoped:
    def test_SEC3_same_operation_id_in_two_tenants_is_independent(
        self, db, tenant_a, tenant_b, customer_factory
    ):
        """The register key is (tenant_id, operation_id), so tenants cannot
        collide with — or observe — one another's operations."""
        ca = customer_factory(tenant_a.ctx, code="TA", price_minor=PRICE)
        cb = customer_factory(tenant_b.ctx, code="TB", price_minor=PRICE)
        shared_op = uuid7()

        for ctx, customer in ((tenant_a.ctx, ca), (tenant_b.ctx, cb)):
            data = RecordServiceInput(customer_id=customer.id, quantity=Decimal("1"))
            outcome = execute_idempotent(
                db,
                ctx,
                operation_id=shared_op,
                op_type="service.record",
                payload={"customer_id": str(customer.id)},
                perform=lambda c=ctx, d=data: record_service(
                    db, c, d, operation_id=shared_op
                ),
            )
            assert outcome.status == "APPLIED"


class TestRouteInventory:
    """A-SEC-3/4: the mechanism notices new scoped routes automatically."""

    EXERCISED = {
        ("GET", "/api/v1/customers"),
        ("POST", "/api/v1/customers"),
        ("GET", "/api/v1/customers/{customer_id}"),
        ("PATCH", "/api/v1/customers/{customer_id}"),
        ("POST", "/api/v1/service/records"),
        ("POST", "/api/v1/service/records/{record_id}/correct"),
        ("POST", "/api/v1/service/records/{record_id}/void"),
        ("GET", "/api/v1/service/day/{service_date}"),
    }

    def test_route_inventory_is_covered(self, app):
        actual = set(tenant_scoped_routes(app))
        missing = actual - self.EXERCISED
        assert missing == set(), (
            "new tenant-scoped route(s) added without isolation coverage: "
            f"{sorted(missing)} — add them to the isolation suite and to EXERCISED"
        )

    def test_openapi_enumerates_the_same_routes(self, app, client):
        schema = client.get("/openapi.json").json()
        from_openapi = {
            (method.upper(), path)
            for path, ops in schema["paths"].items()
            for method in ops
            if path.startswith("/api/v1") and not path.startswith("/api/v1/auth")
        }
        assert from_openapi == set(tenant_scoped_routes(app))

    def test_no_delete_route_exists_anywhere(self, app):
        """AUD-1: no hard-delete path for any business entity."""
        deletes = [
            (m, r.path)
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
            if m == "DELETE"
        ]
        assert deletes == []
