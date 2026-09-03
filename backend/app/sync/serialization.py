"""The commit-order boundary that makes the change feed gap-free (SYN-10).

**The defect this closes.** ``row_version`` is drawn from a sequence *inside* a
transaction, and `nextval` is non-transactional. So allocation order and commit
order are two different orders:

    Tx A  allocates 100 ............................ commits (late)
    Tx B         allocates 101 ... commits (early)
    feed                              sees 101, cursor -> 101
    Tx A                                              commits 100  <- never delivered

A client that had advanced to 101 would never receive 100. That is precisely the
gap SYN-10 forbids, and it needs only two concurrent same-tenant writes plus a
feed read landing between the two commits — which a two-device round (A-SYN-7)
produces.

**The fix.** Every transaction that is about to allocate a ``row_version`` for an
entity the feed carries takes a *tenant-scoped PostgreSQL transaction advisory
lock* first, and holds it until commit or rollback. Within one tenant, feed-
visible writes therefore allocate in the same order they commit, so the set of
committed versions is always a **prefix** of the set of allocated ones. There is
never a committed 101 while an uncommitted 100 exists, because 101 could not have
been allocated until 100's transaction ended.

**Why feed reads need no lock of their own.** They already cannot observe the
dangerous state: it does not exist. A reader sees only committed rows, and the
committed rows are a prefix — so the greatest committed version a reader can see
has nothing missing beneath it, and a cursor set to it can skip nothing. Making
readers take a shared lock would add the same guarantee a second time at the cost
of every pull blocking behind an in-flight write. The property is asserted
directly in ``tests/test_sync_serialization.py``.

**Why an advisory *xact* lock rather than a mutex or a lock table.** It is bound
to the transaction: PostgreSQL releases it on commit **and** on rollback, so no
code path can leak it and no error handler has to remember to. It needs no table.
It is per tenant, so two tenants never wait on each other. And it is held across
the commit itself, which a process-level mutex in an application server cannot
honestly promise.

**Rule for a later package.** When a new entity joins ``SYNC_ENTITIES`` in
``app.sync.changes``, add the ``op_type``\\ s that mutate it to
:data:`FEED_WRITING_OP_TYPES` below. That is the whole contract — the lock is
taken by :func:`app.sync.idempotency.execute_idempotent`, before it claims the
register and before the effect runs, so no individual command has to remember
anything. ``payment``, ``statement`` and ``ledger_entry`` are deliberately absent
because P5's feed does not carry them; adding them here without adding them to
the feed would only serialize writes for no reason.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = [
    "FEED_ADVISORY_LOCK_NAMESPACE",
    "FEED_WRITING_OP_TYPES",
    "serialize_feed_writes",
    "tenant_lock_key",
]

# An arbitrary but fixed namespace, so this lock cannot collide with an advisory
# lock some other part of the system takes later. 0x53594E43 == b"SYNC", clamped
# into a signed int4.
FEED_ADVISORY_LOCK_NAMESPACE = 0x53594E43 - 0x80000000

#: The operations that mutate an entity ``app.sync.changes.SYNC_ENTITIES`` carries.
#:
#: ``tenant`` is absent because nothing mutates a tenant row: it takes its
#: ``row_version`` from the column default when the tenant is provisioned, at
#: which point no client of that tenant exists to have a cursor. If a tenant-
#: configuration write is ever added, its ``op_type`` belongs here.
FEED_WRITING_OP_TYPES: frozenset[str] = frozenset(
    {
        "customer.create",
        "customer.update",
        # P8: an alias write bumps its customer's row_version, so it writes an
        # entity the feed carries and belongs under the same commit-order
        # boundary as any other customer write.
        "customer.alias.add",
        "customer.alias.update",
        "customer.alias.deactivate",
        "service.record",
        "service.skip",
        "service.correct",
        "service.void",
        # P6: payment and statement joined SYNC_ENTITIES.
        "payment.record",
        "payment.void",
        # Issues one immutable statement per billable customer, each with its own
        # row_version, inside the close transaction.
        "billing.close_cycle",
    }
)


def tenant_lock_key(tenant_id: uuid.UUID) -> int:
    """A stable signed int4 derived from the **whole** tenant id.

    Hashed, not sliced. Tenant ids are uuidv7, so their leading bytes are a
    millisecond timestamp: taking the first four would give every tenant
    provisioned in the same millisecond the same key, and unrelated businesses
    would then queue behind one another for no reason. (Found by
    ``test_lock_keys_differ_between_tenants`` — two fixtures created in the same
    millisecond collided.) Hashing all sixteen bytes spreads them regardless.

    A residual collision costs a little unnecessary serialization and nothing
    else — it can never produce a *missing* lock, which is the only failure that
    would matter.
    """
    digest = hashlib.blake2b(tenant_id.bytes, digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


def serialize_feed_writes(session: Session, tenant_id: uuid.UUID) -> None:
    """Take this tenant's feed-write lock for the rest of the transaction.

    Must be called **before** any ``row_version`` for a feed-visible entity is
    allocated. Blocks while another transaction of the same tenant holds it;
    released automatically on commit or rollback.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :tenant)"),
        {"namespace": FEED_ADVISORY_LOCK_NAMESPACE, "tenant": tenant_lock_key(tenant_id)},
    )
