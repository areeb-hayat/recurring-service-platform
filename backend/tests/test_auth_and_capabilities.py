"""Authentication, sessions and the capability map — SEC-5..SEC-8, SEC-11.

Also covers the customer HTTP surface, since it is the thinnest place to prove
the capability checks actually gate the routes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.audit.models import AuditEvent
from app.core.errors import PermissionDeniedError
from app.core.ids import uuid7
from app.core.security import hash_password, hash_refresh_token, verify_password
from app.identity.capabilities import (
    ALL_CAPABILITIES,
    CAPABILITIES,
    PLATFORM_CAPABILITIES,
    TENANT_CAPABILITIES,
    has,
    require,
)
from app.identity.models import Role, UserSession
from app.tenancy.context import Principal
from tests.conftest import OWNER_PASSWORD

pytestmark = pytest.mark.postgres


def _principal(role: str) -> Principal:
    return Principal(
        user_id=uuid7(),
        role=role,
        scope="PLATFORM" if role == Role.PLATFORM_OWNER else "TENANT",
        tenant_id=None if role == Role.PLATFORM_OWNER else uuid7(),
    )


class TestSEC5CapabilityDisjointness:
    def test_SEC5_owner_admin_holds_no_commission_capability(self):
        commission = {c for c in ALL_CAPABILITIES if c.startswith("commission:")}
        assert commission, "expected commission capabilities to exist in the map"
        assert CAPABILITIES[Role.OWNER_ADMIN] & commission == set()

    def test_SEC5_tenant_and_platform_sets_are_disjoint(self):
        assert TENANT_CAPABILITIES & PLATFORM_CAPABILITIES == set()

    def test_SEC5_platform_owner_holds_no_tenant_business_capability(self):
        assert CAPABILITIES[Role.PLATFORM_OWNER] & TENANT_CAPABILITIES == set()

    def test_SEC8_operator_holds_nothing(self):
        assert CAPABILITIES[Role.OPERATOR] == frozenset()
        assert not has(_principal(Role.OPERATOR), "customer:read")
        with pytest.raises(PermissionDeniedError):
            require(_principal(Role.OPERATOR), "service:record")

    def test_SEC8_the_reserved_operator_role_cannot_touch_money(self):
        """P2 adds financial capabilities; the reserved role still grants none."""
        for capability in ("payment:record", "payment:void", "billing:close_cycle"):
            assert not has(_principal(Role.OPERATOR), capability)

    def test_owner_admin_holds_the_p2_financial_capabilities(self):
        owner = _principal(Role.OWNER_ADMIN)
        for capability in (
            "billing:read",
            "billing:close_cycle",
            "payment:record",
            "payment:void",
        ):
            require(owner, capability)

    def test_owner_admin_holds_the_p1_capabilities(self):
        owner = _principal(Role.OWNER_ADMIN)
        for capability in ("customer:read", "customer:write", "service:record", "service:correct"):
            require(owner, capability)

    def test_unknown_capability_fails_loudly(self):
        """A typo must never silently allow."""
        with pytest.raises(ValueError, match="unknown capability"):
            require(_principal(Role.OWNER_ADMIN), "customer:wrtie")


class TestLogin:
    def test_login_returns_tokens(self, client, tenant_a):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": OWNER_PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["role"] == Role.OWNER_ADMIN
        assert body["scope"] == "TENANT"
        assert body["tenant_id"] == str(tenant_a.tenant.id)
        assert body["access_token"] and body["refresh_token"]

    def test_login_rejects_wrong_password(self, client, tenant_a):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_login_rejects_unknown_email_with_the_same_error(self, client, tenant_a):
        """Account existence is not disclosed."""
        unknown = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@alpha.test", "password": OWNER_PASSWORD},
        )
        wrong = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": "nope-nope-nope"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()

    def test_login_validates_email_format(self, client, tenant_a):
        resp = client.post(
            "/api/v1/auth/login", json={"email": "not-an-email", "password": "x" * 12}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION"

    def test_SEC7_customer_cannot_log_in(self, client, tenant_a, customer_factory):
        """SEC-7: a customer has no credentials, so no input can authenticate one."""
        customer = customer_factory(tenant_a.ctx, code="C9", price_minor=100)
        for identifier in (str(customer.id), customer.code, "customer@alpha.test"):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "c@alpha.test", "password": identifier[:20] + "aaaaaaaa"},
            )
            assert resp.status_code in (401, 422)


class TestSEC11Storage:
    def test_SEC11_password_is_argon2_hashed(self):
        digest = hash_password("a-reasonable-password")
        assert digest.startswith("$argon2")
        assert "a-reasonable-password" not in digest
        assert verify_password(digest, "a-reasonable-password")
        assert not verify_password(digest, "wrong")

    def test_SEC11_refresh_token_is_stored_only_as_a_hash(self, client, db, tenant_a):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": OWNER_PASSWORD},
        )
        plaintext = resp.json()["refresh_token"]
        sessions = list(db.execute(select(UserSession)).scalars().all())
        assert len(sessions) == 1
        assert sessions[0].refresh_token_hash != plaintext
        assert sessions[0].refresh_token_hash == hash_refresh_token(plaintext)
        assert len(sessions[0].refresh_token_hash) == 64

    def test_SEC11_refresh_rotates_and_revokes_the_old_token(self, client, db, tenant_a):
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": OWNER_PASSWORD},
        ).json()
        first_refresh = login["refresh_token"]

        rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != first_refresh

        # The presented token is single-use.
        reused = client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
        assert reused.status_code == 401

    def test_SEC11_logout_revokes(self, client, tenant_a):
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": OWNER_PASSWORD},
        ).json()
        assert client.post(
            "/api/v1/auth/logout", json={"refresh_token": login["refresh_token"]}
        ).status_code == 204
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
            ).status_code
            == 401
        )

    def test_expired_access_token_is_rejected(self, client, clock, tenant_a):
        """Deterministic via the injected clock — no sleeping."""
        headers = tenant_a.auth
        assert client.get("/api/v1/customers", headers=headers).status_code == 200
        clock.advance(minutes=61)
        resp = client.get("/api/v1/customers", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "TOKEN_EXPIRED"

    def test_tampered_token_is_rejected(self, client, tenant_a):
        bad = tenant_a.access_token[:-3] + "aaa"
        resp = client.get("/api/v1/customers", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401


class TestAuthAuditing:
    def test_login_success_and_failure_are_audited(self, client, db, tenant_a):
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": OWNER_PASSWORD},
        )
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": "wrong-password-here"},
        )
        actions = {
            r.action for r in db.execute(select(AuditEvent)).scalars().all()
        }
        assert "auth.login_succeeded" in actions
        assert "auth.login_failed" in actions

    def test_audit_never_stores_credentials(self, client, db, tenant_a):
        client.post(
            "/api/v1/auth/login",
            json={"email": "owner@alpha.test", "password": OWNER_PASSWORD},
        )
        for event in db.execute(select(AuditEvent)).scalars().all():
            blob = f"{event.before}{event.after}".lower()
            assert OWNER_PASSWORD.lower() not in blob
            assert "argon2" not in blob
            assert "password_hash" not in blob
            assert "refresh_token" not in blob


class TestCustomerApi:
    def _create(self, client, tenant, **kw):
        body = {
            "operation_id": str(uuid7()),
            "code": kw.pop("code", "C-100"),
            "name": kw.pop("name", "Ahmed"),
            "area": kw.pop("area", "G-10"),
            "unit_price_minor": kw.pop("unit_price_minor", 25000),
            "default_quantity": kw.pop("default_quantity", "2"),
            **kw,
        }
        return client.post("/api/v1/customers", json=body, headers=tenant.auth)

    def test_create_and_read_customer(self, client, tenant_a):
        created = self._create(client, tenant_a)
        assert created.status_code == 201, created.text
        entity = created.json()["entity"]
        assert created.json()["status"] == "APPLIED"
        assert entity["unit_price_minor"] == 25000
        assert entity["default_quantity"] == "2.000"

        fetched = client.get(f"/api/v1/customers/{entity['id']}", headers=tenant_a.auth)
        assert fetched.status_code == 200
        assert fetched.json()["outstanding_minor"] == 0

    def test_create_is_idempotent(self, client, tenant_a):
        body = {
            "operation_id": str(uuid7()),
            "code": "C-200",
            "name": "Bilal",
            "unit_price_minor": 100,
            "default_quantity": "1",
        }
        first = client.post("/api/v1/customers", json=body, headers=tenant_a.auth)
        second = client.post("/api/v1/customers", json=body, headers=tenant_a.auth)
        assert first.status_code == 201
        assert second.json()["status"] == "DUPLICATE"
        assert second.json()["entity"]["id"] == first.json()["entity"]["id"]
        listing = client.get("/api/v1/customers", headers=tenant_a.auth).json()
        assert len(listing["items"]) == 1

    def test_duplicate_code_conflicts(self, client, tenant_a):
        self._create(client, tenant_a, code="DUP")
        again = self._create(client, tenant_a, code="DUP", name="Someone else")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "CUSTOMER_CODE_TAKEN"

    def test_update_is_audited_and_bumps_row_version(self, client, db, tenant_a):
        entity = self._create(client, tenant_a).json()["entity"]
        resp = client.patch(
            f"/api/v1/customers/{entity['id']}",
            json={"operation_id": str(uuid7()), "unit_price_minor": 30000},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 200
        updated = resp.json()["entity"]
        assert updated["unit_price_minor"] == 30000
        assert updated["row_version"] > entity["row_version"]

        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == "customer.updated")
        ).scalar_one()
        assert event.before["unit_price_minor"] == 25000
        assert event.after["unit_price_minor"] == 30000

    def test_optimistic_concurrency_conflict(self, client, tenant_a):
        entity = self._create(client, tenant_a).json()["entity"]
        stale = entity["row_version"] - 1
        resp = client.patch(
            f"/api/v1/customers/{entity['id']}",
            json={
                "operation_id": str(uuid7()),
                "name": "Changed",
                "expected_row_version": stale,
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ROW_VERSION_CONFLICT"

    def test_invalid_phone_is_rejected(self, client, tenant_a):
        resp = self._create(client, tenant_a, code="P1", phone_e164="0300-1234567")
        assert resp.status_code == 422
        assert "phone_e164" in resp.json()["error"]["field_errors"]

    def test_unknown_field_is_rejected(self, client, tenant_a):
        resp = client.post(
            "/api/v1/customers",
            json={
                "operation_id": str(uuid7()),
                "code": "X1",
                "name": "X",
                "sneaky_tenant_id": str(uuid7()),
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 422

    def test_area_filter(self, client, tenant_a):
        self._create(client, tenant_a, code="A1", area="G-10")
        self._create(client, tenant_a, code="A2", area="F-8")
        items = client.get(
            "/api/v1/customers", params={"area": "G-10"}, headers=tenant_a.auth
        ).json()["items"]
        assert [c["code"] for c in items] == ["A1"]
