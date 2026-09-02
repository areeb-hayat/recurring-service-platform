"""Tenant isolation — SEC-1..SEC-6, via HTTP and via direct SQL.

A-SEC-3/4 requires route enumeration from OpenAPI so a newly added scoped route
cannot silently escape the suite. :func:`tenant_scoped_routes` derives the list
from the live app, and :meth:`test_route_inventory_is_covered` fails when a new
route appears that the explicit cases do not exercise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.service.commands import RecordServiceInput, record_service
from app.sync.idempotency import execute_idempotent

pytestmark = pytest.mark.postgres

PRICE = 25000


def _flatten(routes):
    """Yield leaf routes, descending through included routers.

    FastAPI >= 0.141 no longer copies an included router's routes onto
    ``app.routes``; it stores a ``_IncludedRouter`` wrapper holding the original
    router. Walking only the top level therefore returns *nothing* for a mounted
    router, which would make every enumeration-driven guard below pass vacuously
    — the failure mode this helper exists to prevent.
    """
    for route in routes:
        nested = getattr(route, "original_router", None) or getattr(route, "router", None)
        if nested is not None and getattr(nested, "routes", None):
            yield from _flatten(nested.routes)
        else:
            yield route


def _api_routes(app, *, prefix: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in _flatten(app.routes):
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", None)
        if not methods or not path.startswith(prefix):
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return sorted(out)


def tenant_scoped_routes(app) -> list[tuple[str, str]]:
    """Every tenant business route, taken from the app's own route table.

    Auth is excluded because it is unauthenticated by definition, and
    ``/api/v1/platform`` because those routes are the *opposite* assertion: a
    platform token must be accepted there and a tenant token refused. Mixing the
    two lists would make one of the two guarantees untestable.
    """
    return sorted(
        r
        for r in _api_routes(app, prefix="/api/v1")
        if not r[1].startswith("/api/v1/auth")
        and not r[1].startswith("/api/v1/platform")
    )


def platform_scoped_routes(app) -> list[tuple[str, str]]:
    """Every platform-scope route (P0 §15)."""
    return _api_routes(app, prefix="/api/v1/platform")


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


@pytest.fixture
def a_payment(client, tenant_a, a_record):
    body = {
        "operation_id": str(uuid7()),
        "customer_id": a_record["customer_id"],
        "amount_minor": 10000,
        "method": "CASH",
    }
    resp = client.post("/api/v1/payments", json=body, headers=tenant_a.auth)
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

    def test_SEC4_cannot_record_payment_against_other_tenants_customer(
        self, client, tenant_a, tenant_b, a_customer
    ):
        resp = client.post(
            "/api/v1/payments",
            json={
                "operation_id": str(uuid7()),
                "customer_id": a_customer["id"],
                "amount_minor": 1000,
                "method": "CASH",
            },
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_cannot_void_other_tenants_payment(
        self, client, tenant_a, tenant_b, a_payment
    ):
        resp = client.post(
            f"/api/v1/payments/{a_payment['id']}/void",
            json={"operation_id": str(uuid7()), "reason": "hijack"},
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_cannot_close_other_tenants_cycle(
        self, client, tenant_a, tenant_b, a_payment
    ):
        cycle = client.get("/api/v1/billing/cycles", headers=tenant_a.auth).json()["items"][0]
        resp = client.post(
            f"/api/v1/billing/cycles/{cycle['id']}/close",
            json={"operation_id": str(uuid7())},
            headers=tenant_b.auth,
        )
        assert resp.status_code == 404

    def test_SEC4_cannot_read_other_tenants_statement(
        self, client, clock, settings, tenant_a, tenant_b, a_payment
    ):
        from tests._ops import auth_at

        cycle = client.get("/api/v1/billing/cycles", headers=tenant_a.auth).json()["items"][0]
        # period_end is inclusive, so a cycle only closes the day after it.
        boundary = (
            datetime.fromisoformat(cycle["period_end"]) + timedelta(days=1)
        ).replace(hour=12, tzinfo=timezone.utc)
        clock.set(boundary)
        auth_a = auth_at(tenant_a, settings, boundary)
        assert (
            client.post(
                f"/api/v1/billing/cycles/{cycle['id']}/close",
                json={"operation_id": str(uuid7())},
                headers=auth_a,
            ).status_code
            == 200
        )
        statements = client.get(
            f"/api/v1/customers/{a_payment['customer_id']}/statements",
            headers=auth_a,
        ).json()["items"]
        assert len(statements) == 1
        resp = client.get(
            f"/api/v1/statements/{statements[0]['id']}",
            headers=auth_at(tenant_b, settings, boundary),
        )
        assert resp.status_code == 404

    def test_SEC4_cycle_listing_is_scoped(self, client, tenant_a, tenant_b, a_record):
        assert client.get("/api/v1/billing/cycles", headers=tenant_a.auth).json()["items"]
        assert client.get("/api/v1/billing/cycles", headers=tenant_b.auth).json()["items"] == []

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
                .replace("{cycle_id}", str(uuid7()))
                .replace("{statement_id}", str(uuid7()))
                .replace("{payment_id}", str(uuid7()))
            )
            resp = client.request(method, url, json={"operation_id": str(uuid7())}, headers=headers)
            assert resp.status_code in (403, 422), f"{method} {url} -> {resp.status_code}"
            if resp.status_code == 403:
                assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_SEC6_the_platform_surface_is_exactly_the_p3_commission_routes(self, app):
        """P0 §15: P3 adds these four and nothing else."""
        assert set(platform_scoped_routes(app)) == {
            ("GET", "/api/v1/platform/commission/summary"),
            ("GET", "/api/v1/platform/commission/plans"),
            ("POST", "/api/v1/platform/commission/plans"),
            ("POST", "/api/v1/platform/commission/settlements"),
        }

    def test_SEC6_COM7_tenant_token_rejected_on_every_platform_route(
        self, client, app, tenant_a
    ):
        """A-COM-7/8: an owner-admin is refused on every commission route, read
        included, with a *valid* body so the refusal is authorization and not
        request validation."""
        bodies = {
            "/api/v1/platform/commission/plans": {
                "operation_id": str(uuid7()),
                "tenant_id": str(tenant_a.tenant.id),
                "basis": "RECORDED_VALUE",
                "rate_bp": 250,
                "effective_from": "2026-01-01",
            },
            "/api/v1/platform/commission/settlements": {
                "operation_id": str(uuid7()),
                "tenant_id": str(tenant_a.tenant.id),
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "amount_minor": 1000,
            },
        }
        routes = platform_scoped_routes(app)
        assert routes, "platform route enumeration returned nothing"
        for method, path in routes:
            resp = client.request(
                method,
                path,
                params={"tenant_id": str(tenant_a.tenant.id)},
                json=bodies.get(path),
                headers=tenant_a.auth,
            )
            assert resp.status_code in (403, 404), f"{method} {path} -> {resp.status_code}"
            assert resp.json()["error"]["code"] in (
                "PERMISSION_DENIED",
                "NOT_FOUND",
            ), f"{method} {path}"

    def test_SEC6_platform_token_is_accepted_on_the_platform_surface(
        self, client, platform_token, tenant_a
    ):
        """The mirror of the test above: the scope separation cuts both ways."""
        resp = client.get(
            "/api/v1/platform/commission/summary",
            params={"tenant_id": str(tenant_a.tenant.id)},
            headers={"Authorization": f"Bearer {platform_token}"},
        )
        assert resp.status_code == 200, resp.text

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

    def test_SEC2_direct_sql_cross_tenant_payment_is_rejected(
        self, db, tenant_a, tenant_b, customer_factory
    ):
        """PAY-4 at the database level, bypassing the application entirely."""
        customer_a = customer_factory(tenant_a.ctx, code="XP", price_minor=PRICE)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO payment
                      (id, tenant_id, customer_id, amount_minor, method, received_on,
                       status, operation_id, recorded_by_user_id, source, recorded_at)
                    VALUES (gen_random_uuid(), :tenant_b, :customer_a, 500, 'CASH',
                            CURRENT_DATE, 'RECORDED', gen_random_uuid(), :user_b,
                            'ONLINE', now())
                    """
                ),
                {
                    "tenant_b": str(tenant_b.ctx.tenant_id),
                    "customer_a": str(customer_a.id),
                    "user_b": str(tenant_b.owner.id),
                },
            )
        assert "fk_payment_tenant_id_customer_id" in str(exc.value)
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
        ("GET", "/api/v1/tenant/settings"),
        ("GET", "/api/v1/customers"),
        ("POST", "/api/v1/customers"),
        ("GET", "/api/v1/customers/{customer_id}"),
        ("PATCH", "/api/v1/customers/{customer_id}"),
        ("POST", "/api/v1/service/records"),
        ("POST", "/api/v1/service/records/{record_id}/correct"),
        ("POST", "/api/v1/service/records/{record_id}/void"),
        ("GET", "/api/v1/service/day/{service_date}"),
        ("GET", "/api/v1/customers/{customer_id}/statements"),
        ("GET", "/api/v1/billing/cycles"),
        ("POST", "/api/v1/billing/cycles/{cycle_id}/close"),
        ("GET", "/api/v1/statements/{statement_id}"),
        ("POST", "/api/v1/payments"),
        ("POST", "/api/v1/payments/{payment_id}/void"),
    }

    PLATFORM_EXERCISED = {
        ("GET", "/api/v1/platform/commission/summary"),
        ("GET", "/api/v1/platform/commission/plans"),
        ("POST", "/api/v1/platform/commission/plans"),
        ("POST", "/api/v1/platform/commission/settlements"),
    }

    def test_platform_route_inventory_is_covered(self, app):
        """A new platform route cannot escape the COM-7 rejection suite."""
        missing = set(platform_scoped_routes(app)) - self.PLATFORM_EXERCISED
        assert missing == set(), (
            f"new platform route(s) without isolation coverage: {sorted(missing)}"
        )

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
            if path.startswith("/api/v1")
            and not path.startswith("/api/v1/auth")
            and not path.startswith("/api/v1/platform")
        }
        assert from_openapi == set(tenant_scoped_routes(app))

    def test_route_enumeration_is_not_vacuous(self, app):
        """The guard above is only meaningful if it sees the routes at all.

        A framework change that hid mounted routes would silently turn every
        enumeration test in this file green while covering nothing.
        """
        found = tenant_scoped_routes(app)
        assert found, "route enumeration returned nothing — the SEC-3/4 guards are vacuous"
        assert set(found) == self.EXERCISED

    def test_no_delete_route_exists_anywhere(self, app):
        """AUD-1: no hard-delete path for any business entity."""
        deletes = [
            (m, getattr(r, "path", None))
            for r in _flatten(app.routes)
            for m in (getattr(r, "methods", None) or set())
            if m == "DELETE"
        ]
        assert deletes == []
