"""The pull side of sync: a tenant-scoped change feed over ``row_version``.

P0 §7.4. ``row_version`` is drawn from **one shared PostgreSQL sequence** across
every versioned table (P0 §6, SYN-16), so a single integer orders changes across
entity types and doubles as the client's cursor. No timestamp is involved: two
rows written in the same millisecond would be indistinguishable, and a clock is
not a sequence.

**Ordering is total.** Every value comes from one sequence and is therefore
unique across all tables, so ``ORDER BY row_version`` is deterministic without a
tiebreaker.

**The cursor never runs ahead of the data.** It is set to the ``row_version`` of
the last row actually returned — never to the sequence's current value and never
to a page boundary the caller has not received. Replaying an old cursor re-reads
rows the client already has, which is harmless because the client applies rows by
identity (SYN-10: a superset, never a gap).

**No deletions to carry.** Nothing in the financial model is hard-deleted
(FIN-12, AUD-7): a record leaves the active set by changing status, which is an
update and arrives as an ordinary change. So the feed has no tombstone concept
and needs none.

**Entity scope.** ``SYNC_ENTITIES`` is what the V1 client actually stores
offline: the tenant's own configuration, its customers, and its daily service
records — exactly the reads the Daily Register is built from. ``payment``,
``statement`` and ``ledger_entry`` carry ``row_version`` too and will join this
list in the package that builds a screen for them; streaming them now would put
financial rows on devices with nothing to render them and every temptation to
add them up (SYN-9). ``SYNC_FEED_VERSION`` exists so that admitting one is safe:
a client whose stored feed version differs discards its cursor and resynchronises
from zero, which is the only way a newly added entity's *older* rows can reach a
device that is already past them.

Commission never appears here at any version. Those tables carry no
``row_version`` at all (P0 §6, COM-8), so there is no mechanism by which a tenant
could pull one.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.customers.commands import serialize_customer
from app.customers.models import Customer
from app.service.commands import serialize_record
from app.service.models import DailyServiceRecord
from app.tenancy.context import TenantContext
from app.tenancy.models import Tenant
from app.tenancy.settings import tenant_settings

__all__ = [
    "SYNC_FEED_VERSION",
    "current_head",
    "SYNC_ENTITIES",
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "changes_since",
]

# Bump whenever SYNC_ENTITIES changes. Clients treat a different value as
# "resynchronise from zero".
SYNC_FEED_VERSION = 1

SYNC_ENTITIES: tuple[str, ...] = ("tenant", "customer", "daily_service_record")

DEFAULT_PAGE_LIMIT = 500
MAX_PAGE_LIMIT = 1000


def _tenant_rows(
    session: Session, ctx: TenantContext, since: int, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    row = session.execute(
        select(Tenant).where(Tenant.id == ctx.tenant_id, Tenant.row_version > since)
    ).scalar_one_or_none()
    if row is None:
        return []
    return [(row.row_version, str(row.id), tenant_settings(session, ctx))]


def _customer_rows(
    session: Session, ctx: TenantContext, since: int, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    rows = (
        session.execute(
            select(Customer)
            .where(Customer.tenant_id == ctx.tenant_id, Customer.row_version > since)
            .order_by(Customer.row_version)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [(r.row_version, str(r.id), serialize_customer(r, ctx)) for r in rows]


def _service_record_rows(
    session: Session, ctx: TenantContext, since: int, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    rows = (
        session.execute(
            select(DailyServiceRecord)
            .where(
                DailyServiceRecord.tenant_id == ctx.tenant_id,
                DailyServiceRecord.row_version > since,
            )
            .order_by(DailyServiceRecord.row_version)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [(r.row_version, str(r.id), serialize_record(r, ctx)) for r in rows]


_READERS: dict[
    str, Callable[[Session, TenantContext, int, int], list[tuple[int, str, dict[str, Any]]]]
] = {
    "tenant": _tenant_rows,
    "customer": _customer_rows,
    "daily_service_record": _service_record_rows,
}


def current_head(session: Session, ctx: TenantContext) -> int:
    """The greatest ``row_version`` this tenant currently holds, or 0.

    A client that seeds its snapshot from the ordinary read routes needs a cursor
    to continue from. Reading the head **before** those reads makes the handover
    safe in the only direction that matters: anything written afterwards has a
    higher version and is delivered by the feed, so the client sees a superset
    and never a gap (SYN-10).

    Tenant-scoped on purpose. The sequence's own ``last_value`` would be cheaper
    and would also tell every tenant how much every other tenant writes.
    """
    heads = [
        session.execute(
            select(func.max(Tenant.row_version)).where(Tenant.id == ctx.tenant_id)
        ).scalar(),
        session.execute(
            select(func.max(Customer.row_version)).where(
                Customer.tenant_id == ctx.tenant_id
            )
        ).scalar(),
        session.execute(
            select(func.max(DailyServiceRecord.row_version)).where(
                DailyServiceRecord.tenant_id == ctx.tenant_id
            )
        ).scalar(),
    ]
    return max([h for h in heads if h is not None], default=0)


def changes_since(
    session: Session,
    ctx: TenantContext,
    *,
    since: int = 0,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    """Every tenant-scoped change with ``row_version > since``, oldest first.

    Reads ``limit + 1`` from each entity so "is there another page" is answered
    by data rather than by guessing, then merges and truncates. The extra row is
    never returned and never advances the cursor.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    merged: list[tuple[int, str, str, dict[str, Any]]] = []
    for entity in SYNC_ENTITIES:
        for row_version, entity_id, data in _READERS[entity](
            session, ctx, since, limit + 1
        ):
            merged.append((row_version, entity, entity_id, data))

    merged.sort(key=lambda item: item[0])
    has_more = len(merged) > limit
    page = merged[:limit]

    return {
        "since": since,
        # Never past the last row handed over: a cursor that ran ahead of the
        # page would skip whatever it stepped over.
        "cursor": page[-1][0] if page else since,
        "has_more": has_more,
        "head": current_head(session, ctx),
        "feed_version": SYNC_FEED_VERSION,
        "entities": list(SYNC_ENTITIES),
        "changes": [
            {"entity": entity, "id": entity_id, "row_version": row_version, "data": data}
            for row_version, entity, entity_id, data in page
        ],
    }
