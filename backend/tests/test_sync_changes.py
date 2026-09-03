"""P5 pull sync — GET /api/v1/sync/changes — plus SYN-3 over the sync path.

SYN-10: the cursor is monotonic and a replayed cursor yields a superset, never a
gap. The cursor is the shared ``row_version`` sequence (P0 §6, §7.4) — never a
timestamp.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.ids import uuid7
from app.service.models import DailyServiceRecord
from app.sync.changes import SYNC_ENTITIES, SYNC_FEED_VERSION
from app.sync.models import SyncOperation

pytestmark = pytest.mark.postgres

PRICE = 25000
CHANGES_URL = "/api/v1/sync/changes"
SYNC_URL = "/api/v1/sync/operations"


def make_customer(client, fixture, code: str) -> dict:
    resp = client.post(
        "/api/v1/customers",
        json={
            "operation_id": str(uuid7()),
            "code": code,
            "name": f"Customer {code}",
            "unit_price_minor": PRICE,
            "default_quantity": "1",
        },
        headers=fixture.auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity"]


def feed(client, fixture, since: int = 0, limit: int | None = None) -> dict:
    params = {"since": since}
    if limit is not None:
        params["limit"] = limit
    resp = client.get(CHANGES_URL, params=params, headers=fixture.auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestShape:
    def test_feed_declares_its_entities_and_version(self, client, tenant_a):
        body = feed(client, tenant_a)
        assert body["feed_version"] == SYNC_FEED_VERSION
        assert body["entities"] == list(SYNC_ENTITIES)

    def test_a_new_tenant_sees_its_own_tenant_row(self, client, tenant_a):
        body = feed(client, tenant_a)
        assert [c["entity"] for c in body["changes"]] == ["tenant"]
        assert body["changes"][0]["data"]["business_date"] == "2026-03-15"
        assert body["cursor"] == body["changes"][0]["row_version"]

    def test_every_change_carries_entity_id_row_version_and_data(self, client, tenant_a):
        make_customer(client, tenant_a, "C-1")
        for change in feed(client, tenant_a)["changes"]:
            assert set(change) == {"entity", "id", "row_version", "data"}
            assert isinstance(change["row_version"], int)
            uuid.UUID(change["id"])

    def test_customer_change_carries_the_full_serialized_customer(
        self, client, tenant_a
    ):
        created = make_customer(client, tenant_a, "C-1")
        change = next(
            c for c in feed(client, tenant_a)["changes"] if c["entity"] == "customer"
        )
        assert change["data"] == created

    def test_service_record_change_carries_the_full_serialized_record(
        self, client, tenant_a
    ):
        customer = make_customer(client, tenant_a, "C-1")
        resp = client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": customer["id"],
                "quantity": "2",
            },
            headers=tenant_a.auth,
        )
        entity = resp.json()["entity"]
        change = next(
            c
            for c in feed(client, tenant_a)["changes"]
            if c["entity"] == "daily_service_record"
        )
        assert change["data"] == entity

    def test_payment_and_statement_are_carried_from_p6(self, client, tenant_a):
        """P6 admitted them, because P6 builds the screens that render them.

        P5 withheld both on the grounds that streaming financial rows to a device
        with nothing to show them invites a client-side total (SYN-9). The
        customer financial view, the statement list and the payment history are
        that screen, and every figure they show is one the server computed.
        """
        body = feed(client, tenant_a)
        assert "payment" in body["entities"]
        assert "statement" in body["entities"]

    def test_no_ledger_entity_is_exposed(self, client, tenant_a):
        """`ledger_entry` is still absent, and not by oversight.

        Nothing renders a raw ledger row: a statement *is* the presentation of a
        cycle's entries and a balance is derived server-side. Shipping the
        entries would put the one dataset a client could plausibly re-total onto
        the device for no screen at all.
        """
        body = feed(client, tenant_a)
        assert "ledger_entry" not in body["entities"]

    def test_the_feed_version_was_bumped_for_the_new_entities(self, client, tenant_a):
        """Admitting an entity is only safe with a version bump.

        A device already past a payment's `row_version` would otherwise never
        receive it: the feed only ever hands over rows *above* the cursor. A
        different `feed_version` is the client's instruction to discard its
        cursor and resynchronise from zero, which is the only way those older
        rows can arrive.

        P8 raised it again for the other reason a bump exists: no entity joined,
        but every customer row gained an `aliases` field, and rows already on a
        device would otherwise keep the old shape until something unrelated
        changed them. Either way the guarantee is the same — the version is
        strictly above P5's 1, and the feed reports whatever it is.
        """
        from app.sync.changes import SYNC_FEED_VERSION

        assert SYNC_FEED_VERSION > 1, "P6 raised it from P5's 1; P8 raised it again"
        assert feed(client, tenant_a)["feed_version"] == SYNC_FEED_VERSION


class TestHead:
    """The bootstrap handover: seed from the ordinary reads, continue from head."""

    def test_head_is_the_tenants_greatest_row_version(self, client, tenant_a):
        make_customer(client, tenant_a, "C-1")
        body = feed(client, tenant_a, limit=1000)
        assert body["head"] == max(c["row_version"] for c in body["changes"])

    def test_head_does_not_move_when_only_another_tenant_writes(
        self, client, tenant_a, tenant_b
    ):
        before = feed(client, tenant_a)["head"]
        make_customer(client, tenant_b, "B-1")
        assert feed(client, tenant_a)["head"] == before

    def test_seeding_from_head_delivers_everything_written_afterwards(
        self, client, tenant_a
    ):
        head = feed(client, tenant_a, limit=1)["head"]
        later = make_customer(client, tenant_a, "C-1")
        assert [c["id"] for c in feed(client, tenant_a, since=head)["changes"]] == [
            later["id"]
        ]

    def test_head_is_independent_of_the_page_limit(self, client, tenant_a):
        for i in range(5):
            make_customer(client, tenant_a, f"C-{i}")
        assert feed(client, tenant_a, limit=1)["head"] == feed(
            client, tenant_a, limit=1000
        )["head"]


class TestCursor:
    def test_changes_are_ordered_by_row_version(self, client, tenant_a):
        for code in ("C-1", "C-2", "C-3"):
            make_customer(client, tenant_a, code)
        versions = [c["row_version"] for c in feed(client, tenant_a)["changes"]]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)

    def test_since_the_cursor_returns_nothing_new(self, client, tenant_a):
        first = feed(client, tenant_a)
        second = feed(client, tenant_a, since=first["cursor"])
        assert second["changes"] == []
        assert second["cursor"] == first["cursor"]

    def test_cursor_never_moves_backwards(self, client, tenant_a):
        cursor = feed(client, tenant_a)["cursor"]
        for code in ("C-1", "C-2"):
            make_customer(client, tenant_a, code)
            body = feed(client, tenant_a, since=cursor)
            assert body["cursor"] >= cursor
            cursor = body["cursor"]

    def test_only_rows_after_the_cursor_are_returned(self, client, tenant_a):
        first = feed(client, tenant_a)
        made = make_customer(client, tenant_a, "C-1")
        body = feed(client, tenant_a, since=first["cursor"])
        assert [c["id"] for c in body["changes"]] == [made["id"]]

    def test_SYN10_replaying_an_older_cursor_yields_a_superset_never_a_gap(
        self, client, tenant_a
    ):
        first = make_customer(client, tenant_a, "C-1")
        midpoint = feed(client, tenant_a)["cursor"]
        second = make_customer(client, tenant_a, "C-2")

        from_midpoint = {c["id"] for c in feed(client, tenant_a, since=midpoint)["changes"]}
        from_zero = {c["id"] for c in feed(client, tenant_a, since=0)["changes"]}

        assert from_midpoint == {second["id"]}
        # Replaying the older cursor re-delivers what the client already had and
        # loses nothing: a superset, never a gap.
        assert from_midpoint < from_zero
        assert {first["id"], second["id"]} <= from_zero

    def test_an_update_reappears_with_a_higher_row_version(self, client, tenant_a):
        customer = make_customer(client, tenant_a, "C-1")
        cursor = feed(client, tenant_a)["cursor"]
        resp = client.patch(
            f"/api/v1/customers/{customer['id']}",
            json={
                "operation_id": str(uuid7()),
                "name": "Renamed",
                "expected_row_version": customer["row_version"],
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 200, resp.text
        body = feed(client, tenant_a, since=cursor)
        (change,) = [c for c in body["changes"] if c["entity"] == "customer"]
        assert change["data"]["name"] == "Renamed"
        assert change["row_version"] > customer["row_version"]

    def test_a_voided_record_arrives_as_an_update_not_a_disappearance(
        self, client, tenant_a
    ):
        """Nothing is hard-deleted, so the feed needs no tombstones (FIN-12)."""
        customer = make_customer(client, tenant_a, "C-1")
        record = client.post(
            "/api/v1/service/records",
            json={
                "operation_id": str(uuid7()),
                "customer_id": customer["id"],
                "quantity": "2",
            },
            headers=tenant_a.auth,
        ).json()["entity"]
        cursor = feed(client, tenant_a)["cursor"]
        client.post(
            f"/api/v1/service/records/{record['id']}/void",
            json={"operation_id": str(uuid7()), "reason": "wrong customer"},
            headers=tenant_a.auth,
        )
        changes = feed(client, tenant_a, since=cursor)["changes"]
        (voided,) = [c for c in changes if c["id"] == record["id"]]
        assert voided["data"]["status"] == "VOIDED"


class TestPaging:
    def test_a_page_stops_at_the_limit_and_reports_more(self, client, tenant_a):
        for i in range(6):
            make_customer(client, tenant_a, f"C-{i}")
        body = feed(client, tenant_a, limit=3)
        assert len(body["changes"]) == 3
        assert body["has_more"] is True
        assert body["cursor"] == body["changes"][-1]["row_version"]

    def test_paging_walks_every_row_exactly_once(self, client, tenant_a):
        for i in range(7):
            customer = make_customer(client, tenant_a, f"C-{i}")
            client.post(
                "/api/v1/service/records",
                json={
                    "operation_id": str(uuid7()),
                    "customer_id": customer["id"],
                    "quantity": "1",
                },
                headers=tenant_a.auth,
            )
        seen: list[str] = []
        cursor = 0
        for _ in range(50):
            body = feed(client, tenant_a, since=cursor, limit=2)
            seen.extend(c["id"] for c in body["changes"])
            cursor = body["cursor"]
            if not body["has_more"]:
                break
        assert len(seen) == len(set(seen))
        assert set(seen) == {c["id"] for c in feed(client, tenant_a, limit=1000)["changes"]}

    def test_the_cursor_never_advances_past_an_undelivered_row(self, client, tenant_a):
        for i in range(6):
            make_customer(client, tenant_a, f"C-{i}")
        body = feed(client, tenant_a, limit=2)
        delivered = {c["row_version"] for c in body["changes"]}
        assert body["cursor"] == max(delivered)
        # Everything above the cursor is still available on the next page.
        following = feed(client, tenant_a, since=body["cursor"], limit=1000)
        assert following["changes"], "rows above the cursor were skipped"

    def test_limit_is_bounded(self, client, tenant_a):
        assert (
            client.get(
                CHANGES_URL, params={"limit": 100000}, headers=tenant_a.auth
            ).status_code
            == 422
        )

    def test_negative_since_is_refused(self, client, tenant_a):
        assert (
            client.get(CHANGES_URL, params={"since": -1}, headers=tenant_a.auth).status_code
            == 422
        )

    def test_unauthenticated_pull_is_401(self, client):
        assert client.get(CHANGES_URL).status_code == 401


class TestSYN3OverTheSyncPath:
    """A-SYN-3: a fault between the effect and the commit leaves neither behind."""

    def test_fault_before_commit_persists_nothing_and_the_retry_applies(
        self, client, db, tenant_a, monkeypatch
    ):
        import app.sync.operations as operations

        customer = make_customer(client, tenant_a, "C-1")
        envelope = {
            "operation_id": str(uuid7()),
            "op_type": "service.record",
            "payload": {"customer_id": customer["id"], "quantity": "2"},
        }
        real = operations.record_service

        def exploding(*args, **kwargs):
            real(*args, **kwargs)  # the effect happens...
            raise RuntimeError("connection lost before commit")  # ...then the fault

        monkeypatch.setattr(operations, "record_service", exploding)
        resp = client.post(
            SYNC_URL, json={"operations": [envelope]}, headers=tenant_a.auth
        )
        assert resp.status_code == 500

        db.rollback()
        assert (
            db.execute(select(func.count()).select_from(DailyServiceRecord)).scalar_one()
            == 0
        )
        assert (
            db.execute(
                select(func.count())
                .select_from(SyncOperation)
                .where(SyncOperation.operation_id == uuid.UUID(envelope["operation_id"]))
            ).scalar_one()
            == 0
        )

        monkeypatch.setattr(operations, "record_service", real)
        resp = client.post(
            SYNC_URL, json={"operations": [envelope]}, headers=tenant_a.auth
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "APPLIED"


class TestNoRetentionOrPruning:
    """SYN-13: P5 adds no cleanup of the register anywhere."""

    def test_no_pruning_appears_in_the_sync_package(self):
        from tests._source import APP_ROOT, code_only

        for path in sorted((APP_ROOT / "sync").glob("*.py")):
            code = code_only(path).lower().split()
            for marker in ("delete", "truncate", "prune", "purge", "ttl", "expire"):
                assert marker not in code, f"{path.name}: {marker}"
