"""GET /api/v1/tenant/settings — the tenant's own configuration (P4).

The one backend addition P4 needed. These tests pin the three things that make
it safe rather than merely convenient: it is scoped by the token and by nothing
else, it reports the *tenant's* business date rather than the caller's, and it is
read-only.
"""

from __future__ import annotations

import pytest

from app.core.clock import FixedClock
from app.bootstrap import provision_tenant, provision_user
from app.identity.models import Role

pytestmark = pytest.mark.postgres


class TestTenantSettings:
    def test_it_returns_the_tenant_configuration(self, client, tenant_a):
        resp = client.get("/api/v1/tenant/settings", headers=tenant_a.auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == tenant_a.tenant.name
        assert body["currency"] == tenant_a.tenant.currency
        assert body["currency_exponent"] == tenant_a.tenant.currency_exponent
        assert body["unit_label"] == "bottle"
        assert body["timezone"] == "Asia/Karachi"

    def test_the_surface_is_exactly_the_fields_p4_renders(self, client, tenant_a):
        """No speculative field: an unused key claims a designed surface."""
        resp = client.get("/api/v1/tenant/settings", headers=tenant_a.auth)
        assert set(resp.json()) == {
            "name",
            "currency",
            "currency_exponent",
            "unit_label",
            "timezone",
            "business_date",
            "default_quantity",
            "default_unit_price_minor",
        }

    def test_it_leaks_no_tenant_identifier_or_status(self, client, tenant_a):
        """The client never needs — and must never be handed — a tenant_id to send."""
        body = client.get("/api/v1/tenant/settings", headers=tenant_a.auth).json()
        assert "tenant_id" not in body
        assert "id" not in body
        assert "status" not in body

    def test_quantity_crosses_the_wire_as_a_string(self, client, tenant_a):
        """FIN-1: a quantity is never a JSON float, here as anywhere else."""
        body = client.get("/api/v1/tenant/settings", headers=tenant_a.auth).json()
        assert isinstance(body["default_quantity"], str)
        assert isinstance(body["default_unit_price_minor"], int)

    def test_business_date_is_the_tenants_timezone_not_the_callers(
        self, client, db, settings, app
    ):
        """P0 R4: two tenants, one instant, two different business dates.

        20:30 UTC is already the next calendar day in Asia/Karachi (+05:00) and
        still the previous one in America/New_York (-04:00 in March 2026). If the
        route reported the server's date — or the caller's — both would agree.

        The instant matches ``conftest.FROZEN_NOW``'s day: PyJWT validates ``iat``
        against real wall-clock time, so a token minted in the future is rejected
        before the route is ever reached.
        """
        from datetime import datetime, timezone

        from app.core.security import encode_access_token

        instant = datetime(2026, 3, 15, 20, 30, tzinfo=timezone.utc)
        app.state.clock = FixedClock(instant)

        def _seed(slug: str, tz: str, email: str) -> dict[str, str]:
            tenant = provision_tenant(db, slug=slug, name=slug, timezone=tz)
            owner = provision_user(
                db, email=email, password="Sup3r-Secret!", role=Role.OWNER_ADMIN, tenant=tenant
            )
            db.commit()
            token = encode_access_token(
                secret=settings.jwt_secret,
                user_id=str(owner.id),
                scope="TENANT",
                role=Role.OWNER_ADMIN,
                tenant_id=str(tenant.id),
                issued_at=instant,
                expires_in_minutes=60,
            )
            return {"Authorization": f"Bearer {token}"}

        east = _seed("karachi-co", "Asia/Karachi", "owner@karachi.test")
        west = _seed("newyork-co", "America/New_York", "owner@newyork.test")

        east_date = client.get("/api/v1/tenant/settings", headers=east).json()["business_date"]
        west_date = client.get("/api/v1/tenant/settings", headers=west).json()["business_date"]

        assert east_date == "2026-03-16"
        assert west_date == "2026-03-15"

    def test_each_tenant_sees_only_its_own_settings(self, client, tenant_a, tenant_b):
        """SEC-3: the scope comes from the token; there is no parameter to abuse."""
        a = client.get("/api/v1/tenant/settings", headers=tenant_a.auth).json()
        b = client.get("/api/v1/tenant/settings", headers=tenant_b.auth).json()
        assert a["name"] == tenant_a.tenant.name
        assert b["name"] == tenant_b.tenant.name
        assert a["name"] != b["name"]

    def test_it_requires_authentication(self, client):
        resp = client.get("/api/v1/tenant/settings")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_a_platform_token_is_refused(self, client, platform_token):
        """SEC-6: a platform principal has no tenant business authority."""
        resp = client.get(
            "/api/v1/tenant/settings",
            headers={"Authorization": f"Bearer {platform_token}"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_the_route_is_read_only(self, client, tenant_a):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            resp = client.request(
                method, "/api/v1/tenant/settings", json={}, headers=tenant_a.auth
            )
            assert resp.status_code == 405, f"{method} is answered, not refused"
