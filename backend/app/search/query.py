"""The tenant-scoped customer search query.

One statement, built from a closed filter object, parameterised throughout. No
string a caller supplies ever becomes SQL: every value below is bound, and the
only thing this module chooses is which of a fixed set of clauses to include.

**How matching works.** Each way of matching is a *source* — a small SELECT that
yields ``(customer_id, tier, matched_on, matched_value)``. The sources are
``UNION ALL``-ed, the strongest tier per customer is kept, and that tier both
ranks the result and tells the resolver how much the match is worth. A tier is a
small named integer; nothing here computes a percentage or a weighted score,
because a number nobody can reason about is exactly how a search starts
identifying the wrong person.

**``query_text`` is the search box; every other field is a filter.** Only
``query_text`` produces tiers. ``code``, ``phone``, ``area`` and
``name_contains`` are ordinary predicates that narrow the result, and the
structural fields (customer status, outstanding range, service dates) narrow it
further. That split is what keeps the ranking explicable: one input ranks, the
rest include or exclude.

**Where normalization lives.**

* *Names and aliases* — compared on stored normalized columns
  (``customer.normalized_name``, ``customer_alias.normalized``), written by
  :func:`app.search.normalize.normalize_text` at every write path. No SQL
  expression folds case or collapses whitespace: both sides were normalized by
  the same Python function.
* *Code and area* — short identifiers, compared with ``lower(btrim(...))`` on
  both sides, against a matching functional index for the code. These two
  expressions are the only case-folding SQL in the codebase, and they are here
  rather than scattered.
* *Phone* — stored E.164, which is ``+`` followed by digits, so comparing it
  with a digit string needs no column and no table: drop the ``+``.

**Fuzzy matching is optional at runtime.** ``pg_trgm`` supplies
``word_similarity`` and the GIN indexes that make substring search cheap. The P8
migration creates the extension; if a database does not have it, the fuzzy source
is simply not built and search degrades to exact / token / prefix matching rather
than failing. A fuzzy row is always the weakest tier and can never resolve a
customer on its own — see :mod:`app.search.resolver`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, String, and_, func, literal, or_, select, text
from sqlalchemy.orm import Session

from app.billing.models import LedgerEntry
from app.customers.aliases import alias_map_for
from app.customers.models import AliasStatus, Customer, CustomerAlias
from app.search.filters import MAX_SEARCH_LIMIT, CustomerSearchFilter
from app.search.normalize import (
    looks_like_phone,
    normalize_phone,
    normalize_text,
    normalize_tokens,
    phone_suffix,
)
from app.service.models import DailyServiceRecord
from app.tenancy.context import TenantContext

__all__ = [
    "CustomerMatch",
    "FUZZY_THRESHOLD",
    "MatchKind",
    "MatchTier",
    "STRONG_TIER_MIN",
    "search_customers",
    "serialize_match",
    "trigram_available",
]


class MatchTier:
    """How strong a match is. Ordered, named, and deliberately coarse.

    Everything at or above :data:`STRONG_TIER_MIN` is an *identification*: what
    the person typed picks this customer out. Everything below is a
    *suggestion* — worth showing, never worth acting on without a human choosing.
    """

    CODE_EXACT = 100
    PHONE_EXACT = 95
    PHONE_SUFFIX = 90
    NAME_EXACT = 85
    ALIAS_EXACT = 80
    NAME_TOKENS = 75
    ALIAS_TOKENS = 70
    # --- below this line: candidates only, never an identification --------
    NAME_PREFIX = 55
    ALIAS_PREFIX = 50
    NAME_CONTAINS = 45
    ALIAS_CONTAINS = 40
    AREA = 30
    FUZZY = 20
    NONE = 0


class MatchKind:
    CODE = "CODE"
    PHONE = "PHONE"
    NAME = "NAME"
    ALIAS = "ALIAS"
    AREA = "AREA"
    FUZZY = "FUZZY"
    NONE = "NONE"


#: The line between "this is who they meant" and "this might be who they meant".
STRONG_TIER_MIN = MatchTier.ALIAS_TOKENS

#: ``word_similarity(query, stored)`` — the query against the best-matching
#: extent of the stored text, so a typo in a first name still scores against a
#: full name. Fixed here rather than read from the session's
#: ``pg_trgm.word_similarity_threshold`` GUC, so the same query gives the same
#: answer on every connection. Decimal, never float (FIN-1 applies to the whole
#: codebase, not only to money).
FUZZY_THRESHOLD = Decimal("0.6")

_trigram_cache: dict[str, bool] = {}


def trigram_available(session: Session) -> bool:
    """Whether ``pg_trgm`` is installed.

    Cached per database URL: it is a deployment fact, not a per-request one, and
    asking on every keystroke would be a round trip for an answer that cannot
    change while the process runs.
    """
    bind = session.get_bind()
    key = str(getattr(bind, "url", "default"))
    if key not in _trigram_cache:
        _trigram_cache[key] = (
            session.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).first()
            is not None
        )
    return _trigram_cache[key]


def reset_trigram_cache() -> None:
    """Test hook. Never called by application code."""
    _trigram_cache.clear()


@dataclass(frozen=True, slots=True)
class CustomerMatch:
    """One customer, with the reason they matched."""

    customer_id: uuid.UUID
    code: str
    name: str
    area: str | None
    phone_e164: str | None
    whatsapp_e164: str | None
    status: str
    outstanding_minor: int
    tier: int
    matched_on: str
    matched_value: str | None
    aliases: tuple[str, ...] = ()

    @property
    def is_strong(self) -> bool:
        return self.tier >= STRONG_TIER_MIN


def serialize_match(match: CustomerMatch, ctx: TenantContext) -> dict[str, Any]:
    """The wire shape. Enough context to tell two people called Ahmed apart."""
    return {
        "customer_id": str(match.customer_id),
        "code": match.code,
        "name": match.name,
        "area": match.area,
        "phone_e164": match.phone_e164,
        "whatsapp_e164": match.whatsapp_e164,
        "status": match.status,
        "aliases": list(match.aliases),
        # FIN-4: derived by the server, displayed by the client, never recomputed.
        "outstanding_minor": match.outstanding_minor,
        "matched_on": match.matched_on,
        "matched_value": match.matched_value,
        "match_strength": "STRONG" if match.is_strong else "WEAK",
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
    }


# --- match sources ----------------------------------------------------------


def _labelled(customer_id, tier: int, kind: str, value):
    return (
        customer_id.label("customer_id"),
        literal(tier, Integer).label("tier"),
        literal(kind, String).label("matched_on"),
        value.label("matched_value"),
    )


def _whole_word_clause(column, tokens: tuple[str, ...]):
    """Every token appears as a whole word in the normalized column.

    The stored value is space-separated normalized tokens, so padding both sides
    with a space turns "is this a whole word" into an ordinary ``LIKE`` — which
    the trigram GIN index can serve. Word *order* is never required: this is an
    AND of memberships, which is why "Ahmed bhai" and "bhai Ahmed" find the same
    person.
    """
    padded = func.concat(literal(" "), column, literal(" "))
    return and_(*[padded.like(f"% {token} %") for token in tokens])


def _customer_sources(ctx: TenantContext, raw: str, *, fuzzy: bool) -> list[Any]:
    """Match sources rooted at ``customer``."""
    stripped = raw.strip()
    normalized = normalize_text(raw)
    tokens = normalize_tokens(raw)
    scoped = Customer.tenant_id == ctx.tenant_id
    out: list[Any] = []

    def add(tier: int, kind: str, value, clause) -> None:
        out.append(select(*_labelled(Customer.id, tier, kind, value)).where(scoped, clause))

    if stripped:
        add(
            MatchTier.CODE_EXACT,
            MatchKind.CODE,
            Customer.code,
            func.lower(func.btrim(Customer.code)) == stripped.lower(),
        )

    if looks_like_phone(raw):
        digits = normalize_phone(raw)
        suffix = phone_suffix(raw)
        for column in (Customer.phone_e164, Customer.whatsapp_e164):
            add(
                MatchTier.PHONE_EXACT,
                MatchKind.PHONE,
                column,
                func.substr(column, 2) == digits,
            )
            if suffix:
                add(
                    MatchTier.PHONE_SUFFIX,
                    MatchKind.PHONE,
                    column,
                    column.like(f"%{suffix}"),
                )

    if normalized:
        add(
            MatchTier.NAME_EXACT,
            MatchKind.NAME,
            Customer.name,
            Customer.normalized_name == normalized,
        )
        add(
            MatchTier.NAME_TOKENS,
            MatchKind.NAME,
            Customer.name,
            _whole_word_clause(Customer.normalized_name, tokens),
        )
        add(
            MatchTier.NAME_PREFIX,
            MatchKind.NAME,
            Customer.name,
            Customer.normalized_name.like(f"{normalized}%"),
        )
        add(
            MatchTier.NAME_CONTAINS,
            MatchKind.NAME,
            Customer.name,
            Customer.normalized_name.like(f"%{normalized}%"),
        )
        add(
            MatchTier.AREA,
            MatchKind.AREA,
            Customer.area,
            func.lower(func.btrim(func.coalesce(Customer.area, ""))).like(
                f"{stripped.lower()}%"
            ),
        )
        if fuzzy:
            add(
                MatchTier.FUZZY,
                MatchKind.FUZZY,
                Customer.name,
                func.word_similarity(literal(normalized), Customer.normalized_name)
                >= literal(FUZZY_THRESHOLD),
            )
    return out


def _alias_sources(ctx: TenantContext, raw: str, *, fuzzy: bool) -> list[Any]:
    """Match sources rooted at ``customer_alias``. Active aliases only."""
    normalized = normalize_text(raw)
    tokens = normalize_tokens(raw)
    if not normalized:
        return []
    scoped = and_(
        CustomerAlias.tenant_id == ctx.tenant_id,
        CustomerAlias.status == AliasStatus.ACTIVE,
    )
    out: list[Any] = []

    def add(tier: int, kind: str, clause) -> None:
        out.append(
            select(
                *_labelled(CustomerAlias.customer_id, tier, kind, CustomerAlias.alias)
            ).where(scoped, clause)
        )

    add(MatchTier.ALIAS_EXACT, MatchKind.ALIAS, CustomerAlias.normalized == normalized)
    add(
        MatchTier.ALIAS_TOKENS,
        MatchKind.ALIAS,
        _whole_word_clause(CustomerAlias.normalized, tokens),
    )
    add(
        MatchTier.ALIAS_PREFIX,
        MatchKind.ALIAS,
        CustomerAlias.normalized.like(f"{normalized}%"),
    )
    add(
        MatchTier.ALIAS_CONTAINS,
        MatchKind.ALIAS,
        CustomerAlias.normalized.like(f"%{normalized}%"),
    )
    if fuzzy:
        add(
            MatchTier.FUZZY,
            MatchKind.FUZZY,
            func.word_similarity(literal(normalized), CustomerAlias.normalized)
            >= literal(FUZZY_THRESHOLD),
        )
    return out


def _best_match_subquery(session: Session, ctx: TenantContext, raw: str, *, fuzzy: bool):
    """One row per matching customer, carrying its strongest tier.

    ``DISTINCT ON`` is PostgreSQL's, and this codebase is PostgreSQL-only (see
    ``tests/conftest.py``). It keeps the first row per customer under the given
    ordering, which is the strongest tier — with ``matched_on`` breaking a tie
    deterministically so two runs of the same query never disagree about *why* a
    customer matched.
    """
    fuzzy = fuzzy and trigram_available(session)
    sources = _customer_sources(ctx, raw, fuzzy=fuzzy) + _alias_sources(
        ctx, raw, fuzzy=fuzzy
    )
    if not sources:
        return None
    union = sources[0].union_all(*sources[1:]) if len(sources) > 1 else sources[0]
    matches = union.subquery("matches")
    return (
        select(
            matches.c.customer_id,
            matches.c.tier,
            matches.c.matched_on,
            matches.c.matched_value,
        )
        .distinct(matches.c.customer_id)
        .order_by(
            matches.c.customer_id,
            matches.c.tier.desc(),
            matches.c.matched_on,
            matches.c.matched_value,
        )
        .subquery("best")
    )


# --- structural predicates ---------------------------------------------------


def _name_or_alias_contains(ctx: TenantContext, value: str):
    """``name_contains``: the narrow filter, over names and aliases alike."""
    normalized = normalize_text(value)
    alias_exists = (
        select(literal(1))
        .where(
            CustomerAlias.tenant_id == ctx.tenant_id,
            CustomerAlias.customer_id == Customer.id,
            CustomerAlias.status == AliasStatus.ACTIVE,
            CustomerAlias.normalized.like(f"%{normalized}%"),
        )
        .exists()
    )
    return or_(Customer.normalized_name.like(f"%{normalized}%"), alias_exists)


def _phone_predicate(value: str):
    digits = normalize_phone(value)
    suffix = phone_suffix(value)
    clauses = []
    for column in (Customer.phone_e164, Customer.whatsapp_e164):
        clauses.append(func.substr(column, 2) == digits)
        if suffix:
            clauses.append(column.like(f"%{suffix}"))
    return or_(*clauses)


def _service_exists(ctx: TenantContext, *, on_or_after=None, on=None):
    stmt = select(literal(1)).where(
        DailyServiceRecord.tenant_id == ctx.tenant_id,
        DailyServiceRecord.customer_id == Customer.id,
        DailyServiceRecord.status == "ACTIVE",
    )
    if on is not None:
        stmt = stmt.where(DailyServiceRecord.service_date == on)
    if on_or_after is not None:
        stmt = stmt.where(DailyServiceRecord.service_date >= on_or_after)
    return stmt.exists()


def _balances(ctx: TenantContext):
    """Per-customer outstanding, once, in the database (FIN-4)."""
    return (
        select(
            LedgerEntry.customer_id.label("customer_id"),
            func.coalesce(func.sum(LedgerEntry.amount_minor), 0).label(
                "outstanding_minor"
            ),
        )
        .where(LedgerEntry.tenant_id == ctx.tenant_id)
        .group_by(LedgerEntry.customer_id)
        .subquery("balances")
    )


# --- the search --------------------------------------------------------------


def search_customers(
    session: Session, ctx: TenantContext, filt: CustomerSearchFilter
) -> list[CustomerMatch]:
    """Every customer of **this tenant** matching ``filt``, best first.

    Bounded twice — by the filter's own validator and again here — so no caller
    can ask the database for an unbounded result. Aliases for the returned rows
    are loaded in one further statement, never one per row.
    """
    limit = max(1, min(filt.limit, MAX_SEARCH_LIMIT))
    balances = _balances(ctx)
    outstanding = func.coalesce(balances.c.outstanding_minor, 0)

    best = (
        _best_match_subquery(session, ctx, filt.query_text, fuzzy=filt.allow_fuzzy)
        if filt.query_text and filt.query_text.strip()
        else None
    )

    tier = best.c.tier if best is not None else literal(MatchTier.NONE, Integer)
    matched_on = (
        best.c.matched_on if best is not None else literal(MatchKind.NONE, String)
    )
    matched_value = (
        best.c.matched_value if best is not None else literal(None, String)
    )

    stmt = select(
        Customer.id,
        Customer.code,
        Customer.name,
        Customer.area,
        Customer.phone_e164,
        Customer.whatsapp_e164,
        Customer.status,
        outstanding.label("outstanding_minor"),
        tier.label("tier"),
        matched_on.label("matched_on"),
        matched_value.label("matched_value"),
    ).outerjoin(balances, balances.c.customer_id == Customer.id)

    if best is not None:
        stmt = stmt.join(best, best.c.customer_id == Customer.id)

    # SEC-3: the tenant predicate is not optional and not conditional.
    stmt = stmt.where(Customer.tenant_id == ctx.tenant_id)

    if filt.customer_status:
        stmt = stmt.where(Customer.status == filt.customer_status)
    if filt.code:
        stmt = stmt.where(
            func.lower(func.btrim(Customer.code)) == filt.code.strip().lower()
        )
    if filt.phone:
        stmt = stmt.where(_phone_predicate(filt.phone))
    if filt.area:
        stmt = stmt.where(
            func.lower(func.btrim(func.coalesce(Customer.area, "")))
            == filt.area.strip().lower()
        )
    if filt.name_contains and normalize_text(filt.name_contains):
        stmt = stmt.where(_name_or_alias_contains(ctx, filt.name_contains))
    if filt.outstanding_min_minor is not None:
        stmt = stmt.where(outstanding >= filt.outstanding_min_minor)
    if filt.outstanding_max_minor is not None:
        stmt = stmt.where(outstanding <= filt.outstanding_max_minor)
    if filt.has_service_on is not None:
        stmt = stmt.where(_service_exists(ctx, on=filt.has_service_on))
    if filt.no_service_since is not None:
        stmt = stmt.where(~_service_exists(ctx, on_or_after=filt.no_service_since))

    if filt.sort == "NAME" or best is None and filt.sort == "RELEVANCE":
        stmt = stmt.order_by(Customer.name, Customer.id)
    elif filt.sort == "OUTSTANDING":
        stmt = stmt.order_by(outstanding.desc(), Customer.name, Customer.id)
    else:
        # Relevance, then a total order so paging can neither skip nor repeat.
        stmt = stmt.order_by(tier.desc(), Customer.name, Customer.id)

    rows = session.execute(stmt.limit(limit).offset(filt.offset)).all()
    aliases = alias_map_for(session, ctx, [row.id for row in rows])

    return [
        CustomerMatch(
            customer_id=row.id,
            code=row.code,
            name=row.name,
            area=row.area,
            phone_e164=row.phone_e164,
            whatsapp_e164=row.whatsapp_e164,
            status=row.status,
            outstanding_minor=int(row.outstanding_minor or 0),
            tier=int(row.tier),
            matched_on=row.matched_on,
            matched_value=row.matched_value,
            aliases=tuple(aliases.get(row.id, ())),
        )
        for row in rows
    ]
