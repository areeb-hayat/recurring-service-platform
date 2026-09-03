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

**Entity scope.** ``SYNC_ENTITIES`` is what the client actually stores offline:
the tenant's own configuration, its customers, its daily service records — and,
from P6, its ``payment`` and ``statement`` rows, because P6 is the package that
builds the screens that render them. P5 deliberately withheld those two until a
screen existed, on the grounds that streaming financial rows to a device with
nothing to show them invites somebody to add them up (SYN-9); the customer
financial view, the statement list and the payment history are that screen, and
they display server-computed figures verbatim.

``ledger_entry`` is still **absent**, and not by oversight. Nothing renders a raw
ledger row: a statement is the presentation of one cycle's entries and the
customer balance is derived server-side, so shipping the entries themselves would
put the one dataset a client could plausibly re-total onto the device for no
screen at all.

``SYNC_FEED_VERSION`` is what makes admitting an entity safe: a client whose
stored feed version differs discards its cursor and resynchronises from zero,
which is the only way a newly added entity's *older* rows can reach a device
already past them. P6 bumped it to 2; **P8 bumps it to 3**. That resync clears
the snapshot only — the outbox and the issues store are not caches and are never
touched.

**P8 adds no entity, and still needs the bump.** Customer aliases travel inside
the customer payload rather than as a sync entity of their own, so
``SYNC_ENTITIES`` is unchanged. But a device that synchronised before P8 holds
customer rows serialized without an ``aliases`` field, and those rows will not
be sent again until something else changes the customer — so offline search
would silently fail to find a nickname the server knows perfectly well. The
version bump is what re-seeds them. It is also why an alias write bumps its
*customer's* ``row_version`` (``app/customers/aliases.py``): from then on, an
ordinary feed page carries the change.

Commission never appears here at any version. Those tables carry no
``row_version`` at all (P0 §6, COM-8), so there is no mechanism by which a tenant
could pull one.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.models import Statement
from app.billing.statements import serialize_statement
from app.customers.commands import serialize_customers
from app.customers.models import Customer
from app.payments.commands import serialize_payment
from app.payments.models import Payment
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

# Bump whenever SYNC_ENTITIES changes, **or when the serialized shape of an
# entity gains a field a client depends on**. Clients treat a different value as
# "resynchronise from zero". P6 raised it from 1 to 2 when ``payment`` and
# ``statement`` joined; P8 raises it to 3 because every customer row now carries
# ``aliases``, and rows already on a device would otherwise keep the old shape
# until something unrelated changed them.
SYNC_FEED_VERSION = 3

SYNC_ENTITIES: tuple[str, ...] = (
    "tenant",
    "customer",
    "daily_service_record",
    "payment",
    "statement",
)

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
    # Aliases for the whole page in one further statement, never one per row.
    return [
        (row.row_version, str(row.id), payload)
        for row, payload in zip(rows, serialize_customers(session, ctx, rows))
    ]


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


def _payment_rows(
    session: Session, ctx: TenantContext, since: int, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Payments, RECORDED and VOIDED alike.

    A void advances the payment's own ``row_version`` (P0 §7.1, §7.4), so the
    transition arrives as an ordinary update — there is no tombstone and none is
    needed, because nothing is deleted (FIN-12, AUD-7).
    """
    rows = (
        session.execute(
            select(Payment)
            .where(Payment.tenant_id == ctx.tenant_id, Payment.row_version > since)
            .order_by(Payment.row_version)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [(r.row_version, str(r.id), serialize_payment(r, ctx)) for r in rows]


def _statement_rows(
    session: Session, ctx: TenantContext, since: int, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Issued statements. Immutable (FIN-8), so each appears exactly once."""
    rows = (
        session.execute(
            select(Statement)
            .where(Statement.tenant_id == ctx.tenant_id, Statement.row_version > since)
            .order_by(Statement.row_version)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [(r.row_version, str(r.id), serialize_statement(r, ctx)) for r in rows]


_READERS: dict[
    str, Callable[[Session, TenantContext, int, int], list[tuple[int, str, dict[str, Any]]]]
] = {
    "tenant": _tenant_rows,
    "customer": _customer_rows,
    "daily_service_record": _service_record_rows,
    "payment": _payment_rows,
    "statement": _statement_rows,
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
        session.execute(
            select(func.max(Payment.row_version)).where(Payment.tenant_id == ctx.tenant_id)
        ).scalar(),
        session.execute(
            select(func.max(Statement.row_version)).where(
                Statement.tenant_id == ctx.tenant_id
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
