"""Customer identification: RESOLVED, AMBIGUOUS, or NOT_FOUND.

This is the contract every channel shares. P8 drives it from a search box; a
later package drives the same function from a speech transcript or an inbound
text message, and gets the same answer for the same words. There is deliberately
no per-channel matching code for those packages to grow, because two
implementations of "which customer is this?" is two answers to a question that
must have one.

    reference (free text)
            │
            ▼
      CustomerResolver
            │
   ┌────────┼────────────┐
   ▼        ▼            ▼
RESOLVED  AMBIGUOUS   NOT_FOUND
(one id)  (candidates) (nothing)

**The rule, in full.** A reference resolves only when

1. the strongest match is *strong* — a code, a phone number, an exact name, an
   exact alias, or every word of the query appearing as a whole word in a name or
   alias; and
2. exactly one customer holds that strength; and
3. no other customer matches at the same strength.

Anything else is ``AMBIGUOUS`` and returns a short candidate list for a person to
choose from. Nothing weak ever resolves: a prefix, a substring, an area and a
fuzzy match are all suggestions, however far ahead of the field they score. That
is condition 1, and it is the whole reason a typo cannot quietly become a
customer.

**Strict dominance, not "best score wins".** An exact name beats a partial one —
typing "Ahmed" when a customer *is* Ahmed identifies him even though "Ahmed Khan"
also contains the word. Equal strength never chooses: two customers whose names
both contain the word "Ahmed" produce "Which Ahmed?", never a coin toss dressed
up as a ranking.

**Inactive customers are excluded by default.** Identifying somebody who has left
the round, in order to record service for them, is a mistake the system should
not help with; a caller that genuinely wants them asks for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.search.filters import CustomerSearchFilter
from app.search.query import CustomerMatch, MatchTier, search_customers, serialize_match
from app.tenancy.context import TenantContext

__all__ = [
    "DEFAULT_CANDIDATE_LIMIT",
    "MAX_CANDIDATE_LIMIT",
    "CustomerCandidate",
    "CustomerResolution",
    "MatchTier",
    "ResolutionStatus",
    "resolve_customer",
    "serialize_resolution",
]

#: Short on purpose. A list somebody has to read out loud, or tap on a phone
#: while standing at a door, is four or five names — not fifty.
DEFAULT_CANDIDATE_LIMIT = 5
MAX_CANDIDATE_LIMIT = 10


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


#: The candidate type is the match type. Kept as an alias rather than a second
#: near-identical dataclass, so a field can never exist on one and not the other.
CustomerCandidate = CustomerMatch


@dataclass(frozen=True, slots=True)
class CustomerResolution:
    """The answer, and everything needed to act on it or to ask again."""

    status: ResolutionStatus
    query: str
    #: Set **only** when ``status is RESOLVED``. This is the authoritative id.
    customer: CustomerMatch | None = None
    #: Ordered strongest first. Empty for NOT_FOUND; the chosen one for RESOLVED.
    candidates: tuple[CustomerMatch, ...] = ()

    @property
    def customer_id(self):
        return self.customer.customer_id if self.customer is not None else None


def resolve_customer(
    session: Session,
    ctx: TenantContext,
    reference: str,
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    include_inactive: bool = False,
    allow_fuzzy: bool = True,
) -> CustomerResolution:
    """Identify one customer from free text, or refuse to."""
    query = (reference or "").strip()
    if not query:
        return CustomerResolution(status=ResolutionStatus.NOT_FOUND, query=query)

    limit = max(1, min(limit, MAX_CANDIDATE_LIMIT))
    matches = search_customers(
        session,
        ctx,
        CustomerSearchFilter(
            query_text=query,
            customer_status=None if include_inactive else "ACTIVE",
            # One more than asked for, so "there were others" is a fact rather
            # than an inference from a full page.
            limit=limit + 1,
            sort="RELEVANCE",
            allow_fuzzy=allow_fuzzy,
        ),
    )

    if not matches:
        return CustomerResolution(status=ResolutionStatus.NOT_FOUND, query=query)

    best_tier = matches[0].tier
    at_best = [m for m in matches if m.tier == best_tier]
    candidates = tuple(matches[:limit])

    if best_tier >= MatchTier.ALIAS_TOKENS and len(at_best) == 1:
        winner = at_best[0]
        return CustomerResolution(
            status=ResolutionStatus.RESOLVED,
            query=query,
            customer=winner,
            candidates=(winner,),
        )

    # Everything else — several equally good matches, or nothing better than a
    # suggestion — is a question for a person.
    return CustomerResolution(
        status=ResolutionStatus.AMBIGUOUS, query=query, candidates=candidates
    )


def serialize_resolution(
    resolution: CustomerResolution, ctx: TenantContext
) -> dict[str, Any]:
    return {
        "status": resolution.status.value,
        "query": resolution.query,
        "customer": (
            serialize_match(resolution.customer, ctx)
            if resolution.customer is not None
            else None
        ),
        "candidates": [serialize_match(m, ctx) for m in resolution.candidates],
    }
