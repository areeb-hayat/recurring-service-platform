"""GET /api/v1/customers pagination — completeness over a total order (P4 review).

The daily register walks this endpoint to its end, so two things have to be true
and neither was previously tested:

1. the page cap is what the route says it is, and a caller can reach past it;
2. the order is **total**, so consecutive `offset` pages partition the rows.

(2) is the one that bites silently. `customer.name` is not unique, so with
``ORDER BY name`` alone the relative order of tied rows is **unspecified**:
PostgreSQL is free to order them differently for different `OFFSET` values, and a
customer at a page boundary could then be returned twice or not at all. A missing
customer means somebody's delivery is never recorded, with nothing on screen to
say so. The `id` tiebreaker added in the P4 review makes the order total, so the
guarantee no longer depends on which plan the planner picks.

**Honest note on strength.** These tests also pass against the pre-review
``ORDER BY name``: at this data size PostgreSQL happens to return ties in id
order anyway. That is a coincidence of the current plan, not a guarantee, which
is exactly why the tiebreaker is worth having — it turns an accident into a
specification. What these tests pin is the *specified* behaviour going forward;
they are not evidence that the old query was observably broken.
"""

from __future__ import annotations

import pytest

from app.customers.commands import CreateCustomerInput, create_customer

pytestmark = pytest.mark.postgres


def _seed(db, ctx, count: int, *, name_of=lambda i: f"Customer {i:04d}") -> None:
    from app.core.ids import new_id

    for i in range(count):
        create_customer(
            db,
            ctx,
            CreateCustomerInput(
                code=f"C-{i:04d}",
                name=name_of(i),
                phone_e164=None,
                whatsapp_e164=None,
                address=None,
                area=None,
                default_quantity="1",
                unit_price_minor=25000,
            ),
            operation_id=new_id(),
        )
    db.commit()


def _page(client, tenant, *, limit: int, offset: int) -> list[dict]:
    resp = client.get(
        f"/api/v1/customers?limit={limit}&offset={offset}", headers=tenant.auth
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


class TestPageBounds:
    def test_the_default_page_is_100(self, client, db, tenant_a):
        _seed(db, tenant_a.ctx, 120)
        resp = client.get("/api/v1/customers", headers=tenant_a.auth)
        assert len(resp.json()["items"]) == 100

    def test_the_maximum_page_is_500(self, client, db, tenant_a):
        """Asked for more than the cap, the route refuses rather than obeying."""
        _seed(db, tenant_a.ctx, 20)
        assert client.get("/api/v1/customers?limit=501", headers=tenant_a.auth).status_code == 422
        assert client.get("/api/v1/customers?limit=500", headers=tenant_a.auth).status_code == 200

    def test_offset_reaches_past_the_first_page(self, client, db, tenant_a):
        _seed(db, tenant_a.ctx, 250)
        first = _page(client, tenant_a, limit=100, offset=0)
        second = _page(client, tenant_a, limit=100, offset=100)
        third = _page(client, tenant_a, limit=100, offset=200)
        assert [len(first), len(second), len(third)] == [100, 100, 50]

    def test_a_short_page_is_the_only_end_signal(self, client, db, tenant_a):
        """No total, no cursor, no `has_more` — the client cannot rely on one."""
        _seed(db, tenant_a.ctx, 10)
        body = client.get("/api/v1/customers", headers=tenant_a.auth).json()
        assert set(body) == {"items"}


class TestPagingIsComplete:
    def test_walking_the_pages_returns_every_customer_exactly_once(
        self, client, db, tenant_a
    ):
        _seed(db, tenant_a.ctx, 250)

        collected: list[str] = []
        offset = 0
        while True:
            page = _page(client, tenant_a, limit=100, offset=offset)
            collected.extend(c["id"] for c in page)
            if len(page) < 100:
                break
            offset += 100

        assert len(collected) == 250
        assert len(set(collected)) == 250

    def test_duplicate_names_do_not_break_the_page_boundary(self, client, db, tenant_a):
        """Every customer shares one name, so *every* comparison is a tie.

        This is the shape in which an unspecified tie order could drop or repeat
        a row across pages. With the `(name, id)` total order the partition is
        exact by construction.
        """
        _seed(db, tenant_a.ctx, 120, name_of=lambda _i: "Ahmed")

        collected: list[str] = []
        for offset in (0, 40, 80):
            collected.extend(c["id"] for c in _page(client, tenant_a, limit=40, offset=offset))

        assert len(collected) == 120
        assert len(set(collected)) == 120

    def test_the_page_partition_is_stable_across_repeated_reads(self, client, db, tenant_a):
        """Same query, same page: a caller re-reading a page sees the same rows."""
        _seed(db, tenant_a.ctx, 120, name_of=lambda i: "Ahmed" if i % 2 else "Bilal")

        first = [c["id"] for c in _page(client, tenant_a, limit=40, offset=40)]
        again = [c["id"] for c in _page(client, tenant_a, limit=40, offset=40)]
        assert first == again

    def test_paging_a_filtered_list_is_complete_too(self, client, db, tenant_a):
        """The register pages `status=ACTIVE`; the filter must not lose anyone."""
        _seed(db, tenant_a.ctx, 150)

        collected: list[str] = []
        offset = 0
        while True:
            resp = client.get(
                f"/api/v1/customers?status=ACTIVE&limit=100&offset={offset}",
                headers=tenant_a.auth,
            )
            page = resp.json()["items"]
            collected.extend(c["id"] for c in page)
            if len(page) < 100:
                break
            offset += 100

        assert len(set(collected)) == 150


class TestOrdering:
    def test_customers_still_come_back_in_name_order(self, client, db, tenant_a):
        """The tiebreaker did not change the contract, only complete it."""
        _seed(db, tenant_a.ctx, 30)
        names = [c["name"] for c in _page(client, tenant_a, limit=100, offset=0)]
        assert names == sorted(names)

    def test_ties_are_ordered_by_id(self, client, db, tenant_a):
        _seed(db, tenant_a.ctx, 20, name_of=lambda _i: "Ahmed")
        ids = [c["id"] for c in _page(client, tenant_a, limit=100, offset=0)]
        assert ids == sorted(ids)
