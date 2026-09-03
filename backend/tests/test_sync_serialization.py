"""SYN-10 under concurrency: the commit-order gap, and the lock that closes it.

The dangerous timing is not "versions come back unsorted" — they never do. It is
that ``row_version`` is allocated by a non-transactional ``nextval`` *inside* a
transaction, so allocation order and commit order can disagree:

    A allocates 100 ........................ commits late
    B          allocates 101 ... commits early
    feed                            sees 101, cursor -> 101
    A                                          commits 100   <- lost forever

These tests force exactly that interleaving and assert it can no longer happen.
They are real regression tests: remove the advisory lock from
``execute_idempotent`` and :meth:`TestCommitOrder.test_a_second_writer_cannot_allocate_while_the_first_is_uncommitted`
fails immediately.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import select, text

from app.core.db import next_row_version
from app.core.ids import uuid7
from app.customers.models import Customer
from app.service.commands import RecordServiceInput, record_service
from app.sync.changes import changes_since, current_head
from app.sync.idempotency import execute_idempotent
from app.sync.serialization import (
    FEED_WRITING_OP_TYPES,
    serialize_feed_writes,
    tenant_lock_key,
)

pytestmark = pytest.mark.postgres

PRICE = 25000
# Long enough that a *blocked* writer is unambiguously blocked, short enough that
# the suite does not crawl. An unlocked writer finishes in single-digit ms.
WINDOW = 0.75


def _insert_customer(session, tenant_id: uuid.UUID, code: str) -> tuple[uuid.UUID, int]:
    """Insert a feed-visible row and return its id and allocated ``row_version``."""
    version = next_row_version(session)
    customer = Customer(
        tenant_id=tenant_id,
        code=code,
        name=f"Customer {code}",
        unit_price_minor=PRICE,
        row_version=version,
    )
    session.add(customer)
    session.flush()
    return customer.id, version


class TestCommitOrder:
    def test_a_second_writer_cannot_allocate_while_the_first_is_uncommitted(
        self, session_factory, tenant_a
    ):
        """The serialization point itself, and the whole regression.

        Writer A takes the boundary and allocates a version. Writer B tries a
        second feed-visible write for the same tenant. B must not be able to
        allocate — let alone commit — until A is done, because a version B
        allocated and committed first is precisely the version that would strand
        A's behind an advanced cursor.
        """
        tenant_id = tenant_a.tenant.id
        a = session_factory()
        reader = session_factory()
        b_allocated: list[int] = []
        b_finished = threading.Event()
        b_failed: list[BaseException] = []

        def writer_b() -> None:
            session = session_factory()
            try:
                serialize_feed_writes(session, tenant_id)  # blocks while A holds it
                _, version = _insert_customer(session, tenant_id, "B-1")
                b_allocated.append(version)
                session.commit()
                b_finished.set()
            except BaseException as exc:  # noqa: BLE001 - asserted below
                b_failed.append(exc)
                session.rollback()
            finally:
                session.close()

        thread = threading.Thread(target=writer_b, daemon=True)
        try:
            baseline = changes_since(reader, tenant_a.ctx, since=0)["cursor"]

            serialize_feed_writes(a, tenant_id)
            a_id, a_version = _insert_customer(a, tenant_id, "A-1")

            thread.start()
            time.sleep(WINDOW)

            # 1. B is blocked at the boundary: no later version exists at all.
            assert not b_finished.is_set(), (
                "a second same-tenant writer allocated and committed while an "
                "earlier writer was still uncommitted — the SYN-10 gap is open"
            )
            assert b_allocated == []

            # 2. A feed read inside the window sees neither row and does not move
            #    its cursor past A's uncommitted change.
            reader.rollback()  # fresh snapshot
            during = changes_since(reader, tenant_a.ctx, since=baseline)
            assert during["changes"] == []
            assert during["cursor"] == baseline
            assert during["cursor"] < a_version
            assert current_head(reader, tenant_a.ctx) < a_version

            # 3. Release in controlled order.
            a.commit()
            thread.join(timeout=30)
            assert not b_failed, b_failed
            assert b_finished.is_set(), "writer B never completed after A committed"
        finally:
            a.rollback()
            a.close()
            reader.close()
            thread.join(timeout=30)

        b_version = b_allocated[0]
        # Allocation order now *is* commit order.
        assert a_version < b_version

        # 4. Walking the feed from the pre-window cursor delivers both, in order,
        #    with no gap — and re-walking from the new cursor delivers nothing.
        walker = session_factory()
        try:
            after = changes_since(walker, tenant_a.ctx, since=baseline)
            versions = [c["row_version"] for c in after["changes"]]
            ids = {c["id"] for c in after["changes"]}
            assert versions == sorted(versions)
            assert a_version in versions and b_version in versions
            assert str(a_id) in ids
            assert after["cursor"] == max(versions)
            assert changes_since(walker, tenant_a.ctx, since=after["cursor"])["changes"] == []
        finally:
            walker.close()

    def test_paging_one_row_at_a_time_still_delivers_both(self, session_factory, tenant_a):
        """The same two rows, fetched a page at a time: still no gap."""
        writer = session_factory()
        try:
            baseline = changes_since(writer, tenant_a.ctx, since=0)["cursor"]
            for code in ("P-1", "P-2"):
                serialize_feed_writes(writer, tenant_a.tenant.id)
                _insert_customer(writer, tenant_a.tenant.id, code)
                writer.commit()

            seen: list[int] = []
            cursor = baseline
            for _ in range(10):
                page = changes_since(writer, tenant_a.ctx, since=cursor, limit=1)
                seen.extend(c["row_version"] for c in page["changes"])
                cursor = page["cursor"]
                if not page["has_more"]:
                    break
            assert len(seen) == 2
            assert seen == sorted(seen)
        finally:
            writer.rollback()
            writer.close()


class TestTenantsDoNotSerializeEachOther:
    def test_another_tenants_write_is_not_blocked(self, session_factory, tenant_a, tenant_b):
        """The lock is tenant-scoped. Tenant B's round must not wait on tenant A."""
        a = session_factory()
        done = threading.Event()
        failed: list[BaseException] = []

        def other_tenant_writer() -> None:
            session = session_factory()
            try:
                serialize_feed_writes(session, tenant_b.tenant.id)
                _insert_customer(session, tenant_b.tenant.id, "OTHER-1")
                session.commit()
                done.set()
            except BaseException as exc:  # noqa: BLE001
                failed.append(exc)
                session.rollback()
            finally:
                session.close()

        thread = threading.Thread(target=other_tenant_writer, daemon=True)
        try:
            serialize_feed_writes(a, tenant_a.tenant.id)
            _insert_customer(a, tenant_a.tenant.id, "A-2")

            thread.start()
            thread.join(timeout=10)
            assert not failed, failed
            assert done.is_set(), "a different tenant's write waited on this tenant's lock"
        finally:
            a.rollback()
            a.close()
            thread.join(timeout=10)

    def test_lock_keys_differ_between_tenants(self, tenant_a, tenant_b):
        assert tenant_lock_key(tenant_a.tenant.id) != tenant_lock_key(tenant_b.tenant.id)


class TestRollbackReleases:
    def test_rollback_frees_the_boundary_and_poisons_nothing(
        self, session_factory, tenant_a, customer_factory
    ):
        """An abandoned transaction must not leave the tenant unable to write.

        ``pg_advisory_xact_lock`` is released on rollback as well as on commit,
        which is the whole reason it is preferred to any lock the application
        would have to remember to release itself.
        """
        tenant_id = tenant_a.tenant.id
        a = session_factory()
        try:
            serialize_feed_writes(a, tenant_id)
            _insert_customer(a, tenant_id, "R-1")
            a.rollback()
        finally:
            a.close()

        # No lock is left behind anywhere.
        probe = session_factory()
        try:
            held = probe.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND objid = :tenant"
                ),
                {"tenant": tenant_lock_key(tenant_id) % (2**32)},
            ).scalar_one()
            assert held == 0
        finally:
            probe.close()

        # And an ordinary operation still works, through the real command path.
        session = session_factory()
        try:
            customer = customer_factory(tenant_a.ctx, code="R-2", price_minor=PRICE)
            outcome = execute_idempotent(
                session,
                tenant_a.ctx,
                operation_id=uuid7(),
                op_type="service.record",
                payload={"customer_id": str(customer.id), "quantity": "1"},
                perform=lambda: record_service(
                    session,
                    tenant_a.ctx,
                    RecordServiceInput(customer_id=customer.id, quantity="1"),
                    operation_id=uuid7(),
                ),
            )
            assert outcome.status == "APPLIED"
            assert (
                changes_since(session, tenant_a.ctx, since=0)["changes"]
            ), "the feed stopped working after a rolled-back write"
        finally:
            session.rollback()
            session.close()


class TestTheRuleIsDiscoverable:
    """The one thing a later package has to get right."""

    #: Which module actually allocates a ``row_version`` for each feed entity,
    #: and the op types that reach it. This mapping is the whole contract: it is
    #: what a later package has to extend, and what this file exists to pin.
    ENTITY_WRITERS = {
        "customer": ("customers/commands.py", {"customer.create", "customer.update"}),
        "daily_service_record": (
            "service/commands.py",
            {"service.record", "service.skip", "service.correct", "service.void"},
        ),
        # P6. A payment draws a version on insert and advances it on the
        # RECORDED -> VOIDED transition; a statement draws one at issue, which
        # happens only inside a cycle close.
        "payment": ("payments/commands.py", {"payment.record", "payment.void"}),
        "statement": ("billing/statements.py", {"billing.close_cycle"}),
    }

    def test_every_feed_entity_has_a_registered_writing_op_type(self):
        """`SYNC_ENTITIES` and `FEED_WRITING_OP_TYPES` must not drift apart.

        `tenant` is the deliberate exception: nothing mutates a tenant row, so
        there is no op type to register. If that ever changes, this test is where
        the omission surfaces.
        """
        from app.sync.changes import SYNC_ENTITIES

        covered = set(self.ENTITY_WRITERS) | {"tenant"}
        assert set(SYNC_ENTITIES) == covered, (
            "a feed entity has no registered writing op type (or vice versa) — "
            "see app/sync/serialization.py"
        )

    def test_the_feed_writing_operations_are_the_ones_that_bump_a_feed_row_version(self):
        """Every command that writes a feed entity is registered.

        The writing module for each entity is named and checked to contain the
        allocation, so a new mutation path cannot be added to one of them without
        either registering its op type or failing here.
        """
        from tests._source import APP_ROOT

        expected: set[str] = set()
        for relative, ops in self.ENTITY_WRITERS.values():
            source = (APP_ROOT / relative).read_text(encoding="utf-8")
            assert "next_row_version(session)" in source, relative
            expected |= ops

        assert FEED_WRITING_OP_TYPES == expected

    def test_operations_outside_the_feed_do_not_take_the_lock(self):
        """The lock is for feed writers only.

        `ledger_entry` is not a feed entity, and no op type is registered on its
        behalf: registering one without adding the entity to the feed would make
        writes wait for nothing. Commission never appears at any version — those
        tables carry no `row_version` at all.
        """
        from app.sync.changes import SYNC_ENTITIES

        assert "ledger_entry" not in SYNC_ENTITIES
        for op in (
            "commission.plan.create",
            "commission.settlement.record",
            "cost.item.create",
            "cost.rate.create",
            "cost.usage.record",
            "cost.actual.record",
        ):
            assert op not in FEED_WRITING_OP_TYPES

    def test_payment_and_statement_writes_now_take_the_boundary(self):
        """P6's own half of the rule (SYN-10).

        Without these three, the D4 commit-order gap reopens for exactly the rows
        P6 added: a payment allocating early and committing late, behind a
        service record that commits first, is the same race — on financial
        history this time.
        """
        for op in ("payment.record", "payment.void", "billing.close_cycle"):
            assert op in FEED_WRITING_OP_TYPES


class TestTheBoundaryIsWiredIntoRealOperations:
    """The mechanism above is only worth anything if the real path takes it.

    Both writers here go through :func:`execute_idempotent` exactly as a route
    or a sync batch does. Writer A blocks *inside* ``perform`` — after the lock
    is taken and before the commit — which is the window the whole defect lives
    in. Disable the lock in ``execute_idempotent`` and this test fails.
    """

    def test_a_second_operation_cannot_commit_while_the_first_is_in_flight(
        self, session_factory, tenant_a, customer_factory
    ):
        ctx = tenant_a.ctx
        first = customer_factory(ctx, code="W-1", price_minor=PRICE)
        second = customer_factory(ctx, code="W-2", price_minor=PRICE)

        a_inside = threading.Event()
        release_a = threading.Event()
        b_done = threading.Event()
        errors: list[BaseException] = []
        versions: dict[str, int] = {}

        def writer(name: str, customer, gate: bool) -> None:
            session = session_factory()
            operation_id = uuid7()

            def perform():
                if gate:
                    a_inside.set()
                    release_a.wait(timeout=30)
                result, entity_type, entity_id = record_service(
                    session,
                    ctx,
                    RecordServiceInput(customer_id=customer.id, quantity="1"),
                    operation_id=operation_id,
                )
                versions[name] = result["row_version"]
                return result, entity_type, entity_id

            try:
                execute_idempotent(
                    session,
                    ctx,
                    operation_id=operation_id,
                    op_type="service.record",
                    payload={"customer_id": str(customer.id), "quantity": "1"},
                    perform=perform,
                )
                if not gate:
                    b_done.set()
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)
                session.rollback()
            finally:
                session.close()

        a = threading.Thread(target=writer, args=("a", first, True), daemon=True)
        b = threading.Thread(target=writer, args=("b", second, False), daemon=True)
        try:
            a.start()
            assert a_inside.wait(timeout=15), "writer A never reached its effect"

            b.start()
            time.sleep(WINDOW)
            # B has taken no version and committed nothing: it is parked at the
            # boundary behind A. Without the lock it would be finished by now.
            assert not b_done.is_set(), (
                "a second feed-visible operation committed while an earlier one "
                "was still in flight — SYN-10's commit-order boundary is not wired in"
            )
            assert "b" not in versions

            release_a.set()
            a.join(timeout=30)
            b.join(timeout=30)
        finally:
            release_a.set()
            a.join(timeout=30)
            b.join(timeout=30)

        assert not errors, errors
        assert b_done.is_set()
        # Allocated in the order they committed, which is the property the feed
        # depends on.
        assert versions["a"] < versions["b"]

        reader = session_factory()
        try:
            feed = changes_since(reader, ctx, since=0)
            delivered = [c["row_version"] for c in feed["changes"]]
            assert versions["a"] in delivered and versions["b"] in delivered
            assert delivered == sorted(delivered)
        finally:
            reader.close()
