"""The closed customer search filter (P0 §12.1).

A strict Pydantic model with ``extra="forbid"``. Unknown fields, unknown
operators and free-form SQL fragments are refused here, before a query is built
— which is what makes "search cannot be used to name a table or a column" a
property of the type rather than of the code that consumes it.

**This object is not a query language.** Every field maps to exactly one
parameterised clause in :mod:`app.search.query`; there is no operator field, no
expression field and no ordering the caller can invent. That closure is the
reason the same object can later be produced by something other than a person
typing — the validator is the boundary, not the caller's good manners.

**Two P0 §12.1 fields are deliberately absent, and both are recorded rather than
quietly dropped:**

``status`` (``PAID`` / ``PARTIALLY_PAID`` / ``UNPAID``)
    P0 names it, and it is not implemented. It is *derived*, by exactly one
    function (:func:`app.billing.reporting.customer_payment_status`, FIN-11), and
    that function answers for one customer at a time. Filtering a whole table by
    it would mean either calling it per row — an N+1 over the customer
    population — or writing a second, set-based implementation of the derivation,
    which FIN-11's docstring explicitly forbids. ``outstanding_min_minor`` covers
    the product need ("who owes me money") exactly, set-based and authoritative,
    so that is what P8 offers.

``sort`` by anything other than the three values below
    P0 leaves ``sort`` open. It is closed here to an enumeration, because an
    ordering the caller can spell is a column name the caller can spell.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.search.normalize import MAX_QUERY_LENGTH

__all__ = ["CustomerSearchFilter", "DEFAULT_SEARCH_LIMIT", "MAX_SEARCH_LIMIT"]

DEFAULT_SEARCH_LIMIT = 20
#: P0 §12.1 caps a filter at 200 rows. The cap is applied by the model *and*
#: again by the query, so no caller can widen it.
MAX_SEARCH_LIMIT = 200


class CustomerSearchFilter(BaseModel):
    """What a caller may ask for. Nothing else is representable."""

    model_config = ConfigDict(extra="forbid")

    #: The single box: matched against name, alias, code, phone and area.
    query_text: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    #: Name or alias only — the narrower version of ``query_text``.
    name_contains: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    #: Exact customer code, normalized (so ``c-001`` finds ``C-001``).
    code: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    #: A phone or WhatsApp number, however it was typed.
    phone: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    area: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    #: The customer record's own lifecycle, not a payment state.
    customer_status: Literal["ACTIVE", "INACTIVE"] | None = None
    #: Money, in minor units, as everywhere else (FIN-1).
    outstanding_min_minor: int | None = None
    outstanding_max_minor: int | None = None
    #: Served by the ``daily_service_record`` day index.
    has_service_on: date | None = None
    no_service_since: date | None = None
    sort: Literal["RELEVANCE", "NAME", "OUTSTANDING"] = "RELEVANCE"
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT)
    offset: int = Field(default=0, ge=0, le=10_000)
    #: Fuzzy matches are candidates, never authority — see the resolver. They are
    #: opt-out so a caller that wants only certain matches can say so.
    allow_fuzzy: bool = True

    def is_empty(self) -> bool:
        """True when nothing was actually asked for.

        An empty filter is a legitimate "show me everybody", but the caller has
        to be able to tell it apart from a filter that was populated.
        """
        return not any(
            (
                self.query_text,
                self.name_contains,
                self.code,
                self.phone,
                self.area,
                self.customer_status,
                self.outstanding_min_minor is not None,
                self.outstanding_max_minor is not None,
                self.has_service_on,
                self.no_service_since,
            )
        )
