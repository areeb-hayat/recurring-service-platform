"""Shared test fixtures.

Schema and constraint behaviour is asserted against a **real PostgreSQL**
database. SQLite is never substituted: partial unique indexes, composite foreign
keys and CHECK enforcement are precisely what these tests exist to prove, and
SQLite would quietly pass a weaker suite.

Provide ``TEST_DATABASE_URL``, or start the bundled test database:

    docker compose -f docker-compose.test.yml up -d
    export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.bootstrap import provision_tenant, provision_user
from app.core.clock import FixedClock
from app.core.config import Settings
from app.core.db import Base
from app.db_models import ALL_TABLES, import_all_models
from app.identity.models import Role
from app.main import create_app
from app.tenancy.context import TenantContext

import_all_models()

TEST_JWT_SECRET = "test-only-not-a-real-secret-" + "x" * 24
TEST_JOB_SECRET = "test-only-job-secret-" + "y" * 24
OWNER_PASSWORD = "owner-password-for-tests"
PLATFORM_PASSWORD = "platform-password-for-tests"

# A fixed instant well inside a day in Asia/Karachi (UTC+5): 2026-03-15 12:00 local.
FROZEN_NOW = datetime(2026, 3, 15, 7, 0, 0, tzinfo=timezone.utc)


def _database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


def pytest_configure(config):
    """Fail the run loudly when the required PostgreSQL database is absent.

    The schema, constraint and isolation suites are the point of this package. A
    silent skip would turn a missing database into a green run that proves
    nothing, so this aborts collection instead — and SQLite is never substituted.
    """
    if _database_url() is None:
        raise pytest.UsageError(
            "TEST_DATABASE_URL is not set. This suite requires a real "
            "PostgreSQL database and will not fall back to SQLite or skip. "
            "Start it with: docker compose -f docker-compose.test.yml up -d  and set "
            "TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/rsp_test"
        )


@pytest.fixture(scope="session")
def engine():
    url = _database_url()  # guaranteed present by pytest_configure
    eng = create_engine(url, future=True)
    # Rebuild the schema from the migration, not from metadata.create_all — the
    # migration is what production runs, so it is what tests must exercise.
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    _run_migrations(url)
    yield eng
    eng.dispose()


def _run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = url
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def session_factory(engine):
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def clean_tables(engine):
    """Truncate between tests. Cheaper and more realistic than per-test schemas."""
    tables = ", ".join(sorted(ALL_TABLES))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FROZEN_NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=_database_url() or "",
        jwt_secret=TEST_JWT_SECRET,
        internal_job_secret=TEST_JOB_SECRET,
        environment="test",
    )


@pytest.fixture
def comms():
    """The in-memory communication provider. No test ever makes a live call."""
    from app.adapters.comms.mock import MockCommunicationProvider

    return MockCommunicationProvider()


@pytest.fixture
def app(settings, session_factory, clock, comms):
    application = create_app(settings)
    application.state.session_factory = session_factory
    application.state.clock = clock
    # P0 §9: the mock is the test default, injected rather than constructed by
    # the route, so a test can inspect exactly what would have been sent.
    application.state.communication_provider = comms
    return application


@pytest.fixture
def job_headers() -> dict[str, str]:
    return {"X-Job-Secret": TEST_JOB_SECRET}


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# --- seeded data -------------------------------------------------------------


class TenantFixture:
    """One provisioned tenant with an owner-admin, plus helpers."""

    def __init__(self, tenant, owner, ctx: TenantContext, access_token: str) -> None:
        self.tenant = tenant
        self.owner = owner
        self.ctx = ctx
        self.access_token = access_token

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


def _make_tenant(session, settings, clock, *, slug: str, email: str) -> TenantFixture:
    from app.core.security import encode_access_token

    tenant = provision_tenant(
        session,
        slug=slug,
        name=f"{slug.title()} Business",
        timezone="Asia/Karachi",
        unit_label="bottle",
    )
    owner = provision_user(
        session, email=email, password=OWNER_PASSWORD, role=Role.OWNER_ADMIN, tenant=tenant
    )
    session.commit()

    token = encode_access_token(
        secret=settings.jwt_secret,
        user_id=str(owner.id),
        scope="TENANT",
        role=Role.OWNER_ADMIN,
        tenant_id=str(tenant.id),
        issued_at=clock.now_utc(),
        expires_in_minutes=60,
    )
    from app.tenancy.context import Principal

    ctx = TenantContext.build(
        principal=Principal(
            user_id=owner.id, role=Role.OWNER_ADMIN, scope="TENANT", tenant_id=tenant.id
        ),
        tenant=tenant,
        clock=clock,
    )
    return TenantFixture(tenant, owner, ctx, token)


@pytest.fixture
def tenant_a(db, settings, clock) -> TenantFixture:
    return _make_tenant(db, settings, clock, slug="alpha", email="owner@alpha.test")


@pytest.fixture
def tenant_b(db, settings, clock) -> TenantFixture:
    return _make_tenant(db, settings, clock, slug="bravo", email="owner@bravo.test")


@pytest.fixture
def platform_user(db):
    """The platform-scope principal. ``tenant_id`` is NULL by construction."""
    user = provision_user(
        db,
        email="platform@platform.test",
        password=PLATFORM_PASSWORD,
        role=Role.PLATFORM_OWNER,
        tenant=None,
    )
    db.commit()
    return user


@pytest.fixture
def platform_token(platform_user, settings, clock) -> str:
    from app.core.security import encode_access_token

    user = platform_user
    return encode_access_token(
        secret=settings.jwt_secret,
        user_id=str(user.id),
        scope="PLATFORM",
        role=Role.PLATFORM_OWNER,
        tenant_id=None,
        issued_at=clock.now_utc(),
        expires_in_minutes=60,
    )


@pytest.fixture
def customer_factory(db):
    """Create a customer directly, bypassing HTTP, for domain-level tests."""

    def _make(ctx: TenantContext, *, code: str = "C1", price_minor: int = 25000, **kw):
        from app.customers.models import Customer
        from app.core.db import next_row_version

        customer = Customer(
            tenant_id=ctx.tenant_id,
            code=code,
            name=kw.pop("name", f"Customer {code}"),
            area=kw.pop("area", "G-10"),
            default_quantity=kw.pop("default_quantity", Decimal("1.000")),
            unit_price_minor=price_minor,
            row_version=next_row_version(db),
            **kw,
        )
        db.add(customer)
        db.commit()
        return customer

    return _make


def op_id() -> uuid.UUID:
    from app.core.ids import uuid7

    return uuid7()
