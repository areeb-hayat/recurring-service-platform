"""P8 — customer search, aliases and identification.

Three things are under test, and it matters which is which:

* **normalization** — one function, no database, exhaustively pinned;
* **matching** — what a query finds, and in what order;
* **resolution** — when the system is allowed to say "this is the customer", and
  when it must ask instead. That last one is the reason this package exists: a
  resolver that guesses is worse than no resolver, because a wrong customer id
  becomes a wrong delivery, a wrong charge and a wrong balance.
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import event, select, text

from app.audit.models import AuditAction, AuditEvent
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.ids import uuid7
from app.customers.aliases import (
    MAX_ALIASES_PER_CUSTOMER,
    add_alias,
    alias_map_for,
    deactivate_alias,
    list_aliases,
    update_alias,
)
from app.customers.commands import (
    CreateCustomerInput,
    UpdateCustomerInput,
    create_customer,
    serialize_customers,
    update_customer,
)
from app.customers.models import AliasStatus, Customer, CustomerAlias
from app.search.filters import DEFAULT_SEARCH_LIMIT, CustomerSearchFilter
from app.search.normalize import (
    looks_like_phone,
    normalize_phone,
    normalize_text,
    normalize_tokens,
    phone_suffix,
)
from app.search.query import (
    MatchKind,
    MatchTier,
    search_customers,
    serialize_match,
    trigram_available,
)
from app.search.resolver import ResolutionStatus, resolve_customer, serialize_resolution
from tests._ops import do_pay, do_record

pytestmark = pytest.mark.postgres


# --- helpers ----------------------------------------------------------------


def make_customer(db, ctx, code: str, name: str, **kw) -> Customer:
    data, _, customer_id = create_customer(
        db,
        ctx,
        CreateCustomerInput(
            code=code,
            name=name,
            phone_e164=kw.pop("phone_e164", None),
            whatsapp_e164=kw.pop("whatsapp_e164", None),
            address=kw.pop("address", None),
            area=kw.pop("area", None),
            default_quantity=kw.pop("default_quantity", "1"),
            unit_price_minor=kw.pop("unit_price_minor", 25000),
        ),
        operation_id=uuid7(),
    )
    db.commit()
    return db.execute(select(Customer).where(Customer.id == customer_id)).scalar_one()


def give_alias(db, ctx, customer, value: str):
    result = add_alias(db, ctx, customer.id, value, operation_id=uuid7())
    db.commit()
    return result


def find(db, ctx, query: str, **kw):
    return search_customers(
        db, ctx, CustomerSearchFilter(query_text=query, **kw)
    )


def names(matches) -> list[str]:
    return [m.name for m in matches]


# --- 1. normalization -------------------------------------------------------


class TestNormalization:
    """One controlled path. Everything else in P8 compares its output."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Ahmed", "ahmed"),
            ("AHMED BHAI", "ahmed bhai"),
            ("  Ahmed   bhai  ", "ahmed bhai"),
            ("Ahmed-bhai", "ahmed bhai"),
            ("Ahmed_bhai", "ahmed bhai"),
            ("Ahmed  \t bhai\n", "ahmed bhai"),
            ("Áyesha Khán", "ayesha khan"),
            ("C-001", "c 001"),
            ("", ""),
            (None, ""),
            ("!!!", ""),
        ],
    )
    def test_normalize_text(self, raw, expected):
        assert normalize_text(raw) == expected

    def test_all_four_spellings_of_one_nickname_agree(self):
        """The brief's own example, as a single assertion."""
        spellings = ["ahmed bhai", "Ahmed bhai", "AHMED BHAI", "  Ahmed   bhai  "]
        assert len({normalize_text(s) for s in spellings}) == 1

    def test_non_latin_script_survives_normalization(self):
        """Urdu is stored and matched as itself. No transliteration is attempted."""
        normalized = normalize_text("محمد احمد")
        assert normalized
        assert normalized == normalize_text("  محمد   احمد ")
        # And it is deliberately *not* the same key as the Roman spelling: that
        # is what aliases are for.
        assert normalized != normalize_text("Muhammad Ahmed")

    def test_tokens_preserve_order_but_matching_will_not_require_it(self):
        assert normalize_tokens("Chacha Ahmed") == ("chacha", "ahmed")
        assert set(normalize_tokens("Ahmed Chacha")) == set(
            normalize_tokens("Chacha Ahmed")
        )

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+92 300 123-4567", "923001234567"),
            ("0300-1234567", "03001234567"),
            ("+923001234567", "923001234567"),
            ("Ahmed", ""),
        ],
    )
    def test_normalize_phone(self, raw, expected):
        assert normalize_phone(raw) == expected

    def test_phone_suffix_makes_national_and_international_forms_meet(self):
        assert phone_suffix("+923001234567") == phone_suffix("0300-1234567")

    def test_phone_suffix_refuses_a_string_too_short_to_be_specific(self):
        assert phone_suffix("4567") == ""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+923001234567", True),
            ("0300 1234567", True),
            ("Ahmed", False),
            ("Ahmed 3", False),
            ("123", False),
        ],
    )
    def test_looks_like_phone(self, raw, expected):
        assert looks_like_phone(raw) is expected

    def test_normalization_is_bounded(self):
        """A pasted novel is truncated, not rejected and never unbounded."""
        assert len(normalize_text("a" * 10_000)) <= 120

    def test_display_text_is_never_rewritten(self, db, tenant_a):
        """The comparison key sits beside the name; it never replaces it."""
        customer = make_customer(db, tenant_a.ctx, "C-1", "  Muhammad Ahmed Khan ")
        assert customer.name == "Muhammad Ahmed Khan"
        assert customer.normalized_name == "muhammad ahmed khan"


# --- 2. the filter is closed ------------------------------------------------


class TestFilterIsClosed:
    def test_unknown_field_is_refused(self):
        with pytest.raises(Exception):
            CustomerSearchFilter(sql="DROP TABLE customer")

    def test_unknown_sort_is_refused(self):
        with pytest.raises(Exception):
            CustomerSearchFilter(sort="customer.unit_price_minor DESC")

    def test_limit_is_capped(self):
        with pytest.raises(Exception):
            CustomerSearchFilter(limit=100_000)

    def test_a_sql_fragment_is_only_ever_a_string(self, db, tenant_a):
        """Injection is not defended against; it is not representable.

        The value below reaches the database as a bound parameter, so it can only
        ever fail to match a name. The customer table is still there afterwards,
        which is the assertion.
        """
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        assert find(db, tenant_a.ctx, "'; DROP TABLE customer; --") == []
        assert db.execute(select(Customer)).scalars().all()


# --- 3. matching ------------------------------------------------------------


class TestMatching:
    @pytest.fixture
    def book(self, db, tenant_a):
        ctx = tenant_a.ctx
        khan = make_customer(
            db, ctx, "C-001", "Muhammad Ahmed Khan", area="F-10",
            phone_e164="+923001234567",
        )
        ali = make_customer(db, ctx, "C-002", "Ahmed Ali", area="G-11")
        ayesha = make_customer(db, ctx, "C-003", "Ayesha Siddiqui", area="F-10")
        give_alias(db, ctx, khan, "Ahmed bhai")
        give_alias(db, ctx, khan, "Chacha Ahmed")
        give_alias(db, ctx, khan, "Shop wala Ahmed")
        return {"khan": khan, "ali": ali, "ayesha": ayesha}

    def test_exact_canonical_name(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "Muhammad Ahmed Khan")
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.NAME_EXACT
        assert matches[0].matched_on == MatchKind.NAME

    def test_exact_alias(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "Chacha Ahmed")
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.ALIAS_EXACT
        assert matches[0].matched_value == "Chacha Ahmed"

    @pytest.mark.parametrize(
        "query", ["Ahmed bhai", "ahmed bhai", "AHMED BHAI", "  Ahmed   bhai  "]
    )
    def test_case_and_whitespace_do_not_matter(self, db, tenant_a, book, query):
        matches = find(db, tenant_a.ctx, query)
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.ALIAS_EXACT

    def test_word_order_does_not_matter(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "bhai Ahmed")
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.ALIAS_TOKENS

    def test_exact_code(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "c-001")
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.CODE_EXACT

    def test_exact_phone(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "+923001234567")
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.PHONE_EXACT

    def test_phone_typed_in_the_national_form(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "0300-1234567")
        assert matches[0].customer_id == book["khan"].id
        assert matches[0].tier == MatchTier.PHONE_SUFFIX

    def test_whatsapp_number_is_searchable(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(
            db, ctx, "C-9", "Bilal Raza", whatsapp_e164="+923339998877"
        )
        matches = find(db, ctx, "+923339998877")
        assert matches[0].customer_id == customer.id

    def test_partial_name_finds_everyone_who_could_be_meant(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "ahmed")
        assert {m.customer_id for m in matches} == {
            book["khan"].id,
            book["ali"].id,
        }

    def test_prefix_of_a_word_is_a_weak_match(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "ayesh")
        assert matches[0].customer_id == book["ayesha"].id
        assert matches[0].tier == MatchTier.NAME_PREFIX
        assert not matches[0].is_strong

    def test_area_is_searchable(self, db, tenant_a, book):
        matches = find(db, tenant_a.ctx, "G-11")
        assert matches[0].customer_id == book["ali"].id
        assert matches[0].matched_on == MatchKind.AREA

    def test_ranking_is_deterministic_and_repeatable(self, db, tenant_a, book):
        first = [(m.customer_id, m.tier) for m in find(db, tenant_a.ctx, "ahmed")]
        for _ in range(5):
            assert [
                (m.customer_id, m.tier) for m in find(db, tenant_a.ctx, "ahmed")
            ] == first

    def test_stronger_match_sorts_first(self, db, tenant_a):
        """"Ahmed" the exact name outranks "Ahmed Khan" the partial one."""
        ctx = tenant_a.ctx
        exact = make_customer(db, ctx, "C-1", "Ahmed")
        partial = make_customer(db, ctx, "C-2", "Ahmed Khan")
        matches = find(db, ctx, "Ahmed")
        assert matches[0].customer_id == exact.id
        assert matches[1].customer_id == partial.id
        assert matches[0].tier > matches[1].tier

    def test_a_customer_appears_once_however_many_ways_they_match(
        self, db, tenant_a, book
    ):
        matches = find(db, tenant_a.ctx, "Ahmed bhai")
        ids = [m.customer_id for m in matches]
        assert len(ids) == len(set(ids))

    def test_inactive_customers_are_findable_but_marked(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Ahmed Khan")
        update_customer(
            db, ctx, customer.id, UpdateCustomerInput(status="INACTIVE"),
            operation_id=uuid7(),
        )
        db.commit()
        matches = find(db, ctx, "Ahmed Khan")
        assert matches[0].status == "INACTIVE"
        assert find(db, ctx, "Ahmed Khan", customer_status="ACTIVE") == []

    def test_empty_query_returns_the_book_in_name_order(self, db, tenant_a, book):
        matches = search_customers(db, tenant_a.ctx, CustomerSearchFilter())
        assert names(matches) == ["Ahmed Ali", "Ayesha Siddiqui", "Muhammad Ahmed Khan"]
        assert all(m.tier == MatchTier.NONE for m in matches)

    def test_result_limit_is_honoured(self, db, tenant_a, book):
        assert len(find(db, tenant_a.ctx, "ahmed", limit=1)) == 1


# --- 4. fuzzy ---------------------------------------------------------------


class TestFuzzyMatching:
    def test_the_extension_is_installed_by_the_migration(self, db):
        assert trigram_available(db) is True

    def test_a_typo_surfaces_a_candidate(self, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        matches = find(db, tenant_a.ctx, "Ahmd")
        assert [m.customer_id for m in matches] == [customer.id]
        assert matches[0].tier == MatchTier.FUZZY

    def test_a_fuzzy_match_is_never_strong(self, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        matches = find(db, tenant_a.ctx, "Ahmd")
        assert not matches[0].is_strong
        assert serialize_match(matches[0], tenant_a.ctx)["match_strength"] == "WEAK"

    def test_fuzzy_can_be_switched_off(self, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        assert find(db, tenant_a.ctx, "Ahmd", allow_fuzzy=False) == []

    def test_unrelated_words_do_not_fuzzy_match(self, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        assert find(db, tenant_a.ctx, "Zulfiqar") == []

    def test_the_threshold_is_ours_not_the_sessions(self, db, tenant_a):
        """A changed session GUC must not change what search returns.

        The comparison is written into the SQL, so a connection that arrives with
        a different ``pg_trgm.word_similarity_threshold`` gets the same answer.
        """
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        db.execute(text("SET pg_trgm.word_similarity_threshold = 0.01"))
        assert find(db, tenant_a.ctx, "Zulfiqar") == []
        db.execute(text("RESET pg_trgm.word_similarity_threshold"))


# --- 5. resolution ----------------------------------------------------------


class TestResolution:
    """The contract P9 and P10 will reuse. Never guess."""

    def test_one_strong_match_resolves(self, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        result = resolve_customer(db, tenant_a.ctx, "Ahmed Khan")
        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == customer.id

    def test_an_alias_resolves(self, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, tenant_a.ctx, customer, "Ahmed bhai")
        result = resolve_customer(db, tenant_a.ctx, "ahmed BHAI")
        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == customer.id

    def test_a_single_first_name_resolves_when_only_one_person_has_it(
        self, db, tenant_a
    ):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        make_customer(db, tenant_a.ctx, "C-2", "Ayesha Siddiqui")
        result = resolve_customer(db, tenant_a.ctx, "Ahmed")
        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == customer.id

    def test_two_ahmeds_are_ambiguous(self, db, tenant_a):
        """The brief's example: "Which Ahmed?", never a silent pick."""
        khan = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan", area="F-10")
        ali = make_customer(db, tenant_a.ctx, "C-2", "Ahmed Ali", area="G-11")
        result = resolve_customer(db, tenant_a.ctx, "Ahmed")
        assert result.status is ResolutionStatus.AMBIGUOUS
        assert result.customer is None
        assert {c.customer_id for c in result.candidates} == {khan.id, ali.id}
        assert {c.area for c in result.candidates} == {"F-10", "G-11"}

    def test_two_customers_sharing_a_nickname_are_ambiguous(self, db, tenant_a):
        one = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        two = make_customer(db, tenant_a.ctx, "C-2", "Ahmed Raza")
        give_alias(db, tenant_a.ctx, one, "Ahmed bhai")
        give_alias(db, tenant_a.ctx, two, "Ahmed bhai")
        result = resolve_customer(db, tenant_a.ctx, "Ahmed bhai")
        assert result.status is ResolutionStatus.AMBIGUOUS
        assert {c.customer_id for c in result.candidates} == {one.id, two.id}

    def test_nothing_matching_is_not_found(self, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        result = resolve_customer(db, tenant_a.ctx, "Zulfiqar Chaudhry")
        assert result.status is ResolutionStatus.NOT_FOUND
        assert result.candidates == ()

    def test_a_fuzzy_match_never_resolves_however_far_ahead_it_is(self, db, tenant_a):
        """The single most important assertion in this file."""
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        result = resolve_customer(db, tenant_a.ctx, "Ahmd")
        assert result.status is ResolutionStatus.AMBIGUOUS
        assert result.customer is None
        assert len(result.candidates) == 1
        assert result.candidates[0].tier == MatchTier.FUZZY

    def test_a_prefix_never_resolves_on_its_own(self, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        result = resolve_customer(db, tenant_a.ctx, "Ahm")
        assert result.status is ResolutionStatus.AMBIGUOUS
        assert result.customer is None

    def test_an_exact_name_beats_a_partial_one(self, db, tenant_a):
        exact = make_customer(db, tenant_a.ctx, "C-1", "Ahmed")
        make_customer(db, tenant_a.ctx, "C-2", "Ahmed Khan")
        result = resolve_customer(db, tenant_a.ctx, "Ahmed")
        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == exact.id

    def test_a_code_resolves_immediately(self, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-001", "Ahmed Khan")
        make_customer(db, tenant_a.ctx, "C-002", "Ahmed Ali")
        result = resolve_customer(db, tenant_a.ctx, "c-001")
        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == customer.id

    def test_a_phone_number_resolves(self, db, tenant_a):
        customer = make_customer(
            db, tenant_a.ctx, "C-1", "Ahmed Khan", phone_e164="+923001234567"
        )
        make_customer(db, tenant_a.ctx, "C-2", "Ahmed Ali")
        result = resolve_customer(db, tenant_a.ctx, "0300 1234567")
        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == customer.id

    def test_an_inactive_customer_is_not_identified_by_default(self, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        update_customer(
            db, tenant_a.ctx, customer.id, UpdateCustomerInput(status="INACTIVE"),
            operation_id=uuid7(),
        )
        db.commit()
        assert (
            resolve_customer(db, tenant_a.ctx, "Ahmed Khan").status
            is ResolutionStatus.NOT_FOUND
        )
        assert (
            resolve_customer(
                db, tenant_a.ctx, "Ahmed Khan", include_inactive=True
            ).status
            is ResolutionStatus.RESOLVED
        )

    def test_blank_reference_is_not_found_and_touches_nothing(self, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        assert (
            resolve_customer(db, tenant_a.ctx, "   ").status
            is ResolutionStatus.NOT_FOUND
        )

    def test_candidate_list_is_bounded(self, db, tenant_a):
        for i in range(12):
            make_customer(db, tenant_a.ctx, f"C-{i}", f"Ahmed Number{i}")
        result = resolve_customer(db, tenant_a.ctx, "Ahmed", limit=5)
        assert result.status is ResolutionStatus.AMBIGUOUS
        assert len(result.candidates) == 5

    def test_resolution_is_deterministic(self, db, tenant_a):
        for i in range(6):
            make_customer(db, tenant_a.ctx, f"C-{i}", f"Ahmed Number{i}")
        first = [
            c.customer_id for c in resolve_customer(db, tenant_a.ctx, "Ahmed").candidates
        ]
        for _ in range(5):
            assert [
                c.customer_id
                for c in resolve_customer(db, tenant_a.ctx, "Ahmed").candidates
            ] == first

    def test_serialized_resolution_carries_what_a_person_needs_to_choose(
        self, db, tenant_a
    ):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan", area="F-10")
        make_customer(db, tenant_a.ctx, "C-2", "Ahmed Ali", area="G-11")
        body = serialize_resolution(
            resolve_customer(db, tenant_a.ctx, "Ahmed"), tenant_a.ctx
        )
        assert body["status"] == "AMBIGUOUS"
        assert body["customer"] is None
        for candidate in body["candidates"]:
            assert {"name", "code", "area", "matched_on", "match_strength"} <= set(
                candidate
            )


# --- 6. structural filters --------------------------------------------------


class TestStructuralFilters:
    def test_outstanding_range_uses_the_ledger(self, db, tenant_a):
        ctx = tenant_a.ctx
        owing = make_customer(db, ctx, "C-1", "Ahmed Khan")
        paid = make_customer(db, ctx, "C-2", "Ayesha Siddiqui")
        do_record(db, ctx, owing, quantity="2")
        do_record(db, ctx, paid, quantity="2")
        do_pay(db, ctx, paid, 50000)
        db.commit()

        matches = search_customers(
            db, ctx, CustomerSearchFilter(outstanding_min_minor=1)
        )
        assert [m.customer_id for m in matches] == [owing.id]
        assert matches[0].outstanding_minor == 50000

    def test_sort_by_outstanding(self, db, tenant_a):
        ctx = tenant_a.ctx
        small = make_customer(db, ctx, "C-1", "Aaa Small")
        large = make_customer(db, ctx, "C-2", "Zzz Large")
        do_record(db, ctx, small, quantity="1")
        do_record(db, ctx, large, quantity="4")
        db.commit()
        matches = search_customers(db, ctx, CustomerSearchFilter(sort="OUTSTANDING"))
        assert [m.customer_id for m in matches] == [large.id, small.id]

    def test_area_filter_is_exact_and_case_insensitive(self, db, tenant_a):
        ctx = tenant_a.ctx
        here = make_customer(db, ctx, "C-1", "Ahmed Khan", area="G-10")
        make_customer(db, ctx, "C-2", "Ayesha Siddiqui", area="F-10")
        matches = search_customers(db, ctx, CustomerSearchFilter(area="g-10"))
        assert [m.customer_id for m in matches] == [here.id]

    def test_has_service_on_and_no_service_since(self, db, tenant_a):
        ctx = tenant_a.ctx
        served = make_customer(db, ctx, "C-1", "Ahmed Khan")
        make_customer(db, ctx, "C-2", "Ayesha Siddiqui")
        do_record(db, ctx, served, quantity="1")
        db.commit()
        today = ctx.today

        matches = search_customers(db, ctx, CustomerSearchFilter(has_service_on=today))
        assert [m.customer_id for m in matches] == [served.id]

        quiet = search_customers(
            db, ctx, CustomerSearchFilter(no_service_since=today)
        )
        assert served.id not in {m.customer_id for m in quiet}

    def test_name_contains_searches_aliases_too(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Shop wala Ahmed")
        make_customer(db, ctx, "C-2", "Ayesha Siddiqui")
        matches = search_customers(db, ctx, CustomerSearchFilter(name_contains="wala"))
        assert [m.customer_id for m in matches] == [customer.id]


# --- 7. aliases -------------------------------------------------------------


class TestAliases:
    def test_a_customer_may_have_several(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        for value in ("Ahmed", "Ahmed bhai", "Chacha Ahmed", "Shop wala Ahmed"):
            give_alias(db, ctx, customer, value)
        assert len(list_aliases(db, ctx, customer.id)) == 4

    def test_alias_text_is_kept_exactly_as_typed(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "  Chacha   Ahmed ")
        row = list_aliases(db, ctx, customer.id)[0]
        assert row.alias == "Chacha   Ahmed"
        assert row.normalized == "chacha ahmed"

    def test_duplicate_alias_for_one_customer_is_refused(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Ahmed bhai")
        with pytest.raises(ConflictError):
            add_alias(db, ctx, customer.id, "AHMED   BHAI", operation_id=uuid7())

    def test_two_customers_may_share_an_alias(self, db, tenant_a):
        """The ambiguity case has to be *representable*, or it cannot be handled."""
        ctx = tenant_a.ctx
        one = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        two = make_customer(db, ctx, "C-2", "Ahmed Raza")
        give_alias(db, ctx, one, "Ahmed bhai")
        give_alias(db, ctx, two, "Ahmed bhai")
        assert list_aliases(db, ctx, one.id)[0].alias == "Ahmed bhai"
        assert list_aliases(db, ctx, two.id)[0].alias == "Ahmed bhai"

    def test_correcting_a_typo_keeps_the_row(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Ahmd bhai")
        row = list_aliases(db, ctx, customer.id)[0]
        update_alias(db, ctx, customer.id, row.id, "Ahmed bhai", operation_id=uuid7())
        db.commit()
        after = list_aliases(db, ctx, customer.id)
        assert len(after) == 1
        assert after[0].id == row.id
        assert after[0].alias == "Ahmed bhai"
        assert after[0].normalized == "ahmed bhai"

    def test_a_retired_alias_stops_matching_but_stays(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Chacha Ahmed")
        row = list_aliases(db, ctx, customer.id)[0]
        deactivate_alias(
            db, ctx, customer.id, row.id, reason="he asked us to stop",
            operation_id=uuid7(),
        )
        db.commit()
        assert list_aliases(db, ctx, customer.id) == []
        assert len(list_aliases(db, ctx, customer.id, include_inactive=True)) == 1
        assert find(db, ctx, "Chacha Ahmed") == []

    def test_readding_a_retired_alias_reactivates_the_same_row(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Chacha Ahmed")
        row = list_aliases(db, ctx, customer.id)[0]
        deactivate_alias(db, ctx, customer.id, row.id, operation_id=uuid7())
        db.commit()
        give_alias(db, ctx, customer, "Chacha Ahmed")
        rows = list_aliases(db, ctx, customer.id, include_inactive=True)
        assert len(rows) == 1
        assert rows[0].id == row.id
        assert rows[0].status == AliasStatus.ACTIVE
        assert rows[0].deactivated_at is None

    def test_deactivating_twice_is_not_an_error_and_writes_no_second_event(
        self, db, tenant_a
    ):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Chacha Ahmed")
        row = list_aliases(db, ctx, customer.id)[0]
        deactivate_alias(db, ctx, customer.id, row.id, operation_id=uuid7())
        db.commit()
        deactivate_alias(db, ctx, customer.id, row.id, operation_id=uuid7())
        db.commit()
        events = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CUSTOMER_ALIAS_DEACTIVATED
            )
        ).scalars().all()
        assert len(events) == 1

    def test_blank_and_punctuation_only_aliases_are_refused(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        for bad in ("", "   ", "!!!", "---"):
            with pytest.raises(ValidationFailed):
                add_alias(db, ctx, customer.id, bad, operation_id=uuid7())

    def test_alias_count_is_bounded(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        for i in range(MAX_ALIASES_PER_CUSTOMER):
            give_alias(db, ctx, customer, f"Nickname {i}")
        with pytest.raises(ValidationFailed):
            add_alias(db, ctx, customer.id, "One too many", operation_id=uuid7())

    def test_alias_history_has_no_delete_path(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Chacha Ahmed")
        row = list_aliases(db, ctx, customer.id)[0]
        with pytest.raises(Exception) as excinfo:
            db.execute(
                text("DELETE FROM customer_alias WHERE id = :id"), {"id": row.id}
            )
        assert "no delete path" in str(excinfo.value)
        db.rollback()

    def test_every_alias_write_is_audited_with_before_and_after(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Ahmd bhai")
        row = list_aliases(db, ctx, customer.id)[0]
        update_alias(db, ctx, customer.id, row.id, "Ahmed bhai", operation_id=uuid7())
        db.commit()

        events = db.execute(
            select(AuditEvent)
            .where(AuditEvent.entity_type == "customer_alias")
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        ).scalars().all()
        actions = [e.action for e in events]
        assert AuditAction.CUSTOMER_ALIAS_ADDED in actions
        assert AuditAction.CUSTOMER_ALIAS_UPDATED in actions
        correction = [
            e for e in events if e.action == AuditAction.CUSTOMER_ALIAS_UPDATED
        ][0]
        assert correction.before["alias"] == "Ahmd bhai"
        assert correction.after["alias"] == "Ahmed bhai"
        assert correction.actor_user_id == tenant_a.owner.id

    def test_an_alias_write_bumps_the_customers_row_version(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        before = customer.row_version
        give_alias(db, ctx, customer, "Ahmed bhai")
        db.refresh(customer)
        assert customer.row_version > before

    def test_alias_for_a_missing_customer_is_404_not_403(self, db, tenant_a):
        with pytest.raises(NotFoundError):
            add_alias(db, tenant_a.ctx, uuid7(), "Ahmed", operation_id=uuid7())


# --- 8. the customer payload ------------------------------------------------


class TestCustomerPayloadCarriesAliases:
    def test_serialize_customer_includes_active_aliases_only(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, ctx, customer, "Ahmed bhai")
        give_alias(db, ctx, customer, "Chacha Ahmed")
        row = [
            a for a in list_aliases(db, ctx, customer.id) if a.alias == "Chacha Ahmed"
        ][0]
        deactivate_alias(db, ctx, customer.id, row.id, operation_id=uuid7())
        db.commit()

        payload = serialize_customers(db, ctx, [customer])[0]
        assert payload["aliases"] == ["Ahmed bhai"]

    def test_a_customer_with_no_alias_serializes_an_empty_list(self, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        assert serialize_customers(db, tenant_a.ctx, [customer])[0]["aliases"] == []

    def test_renaming_a_customer_keeps_the_comparison_key_in_step(self, db, tenant_a):
        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Ahmed Kahn")
        update_customer(
            db, ctx, customer.id, UpdateCustomerInput(name="Ahmed Khan"),
            operation_id=uuid7(),
        )
        db.commit()
        db.refresh(customer)
        assert customer.normalized_name == "ahmed khan"
        assert find(db, ctx, "Ahmed Khan")[0].tier == MatchTier.NAME_EXACT
        assert find(db, ctx, "Ahmed Kahn", allow_fuzzy=False) == []


# --- 9. tenant isolation ----------------------------------------------------


class TestTenantIsolation:
    def test_search_never_crosses_a_tenant(self, db, tenant_a, tenant_b):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        make_customer(db, tenant_b.ctx, "C-1", "Ahmed Khan")
        a_matches = find(db, tenant_a.ctx, "Ahmed Khan")
        b_matches = find(db, tenant_b.ctx, "Ahmed Khan")
        assert len(a_matches) == 1 and len(b_matches) == 1
        assert a_matches[0].customer_id != b_matches[0].customer_id

    def test_aliases_never_cross_a_tenant(self, db, tenant_a, tenant_b):
        theirs = make_customer(db, tenant_b.ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, tenant_b.ctx, theirs, "Chacha Ahmed")
        make_customer(db, tenant_a.ctx, "C-1", "Ayesha Siddiqui")
        assert find(db, tenant_a.ctx, "Chacha Ahmed") == []
        assert alias_map_for(db, tenant_a.ctx, [theirs.id]) == {}

    def test_resolution_never_crosses_a_tenant(self, db, tenant_a, tenant_b):
        make_customer(db, tenant_b.ctx, "C-1", "Ahmed Khan")
        assert (
            resolve_customer(db, tenant_a.ctx, "Ahmed Khan").status
            is ResolutionStatus.NOT_FOUND
        )

    def test_an_alias_cannot_name_another_tenants_customer(
        self, db, tenant_a, tenant_b
    ):
        theirs = make_customer(db, tenant_b.ctx, "C-1", "Ahmed Khan")
        # Through the domain: 404, because the customer is not this tenant's.
        with pytest.raises(NotFoundError):
            add_alias(db, tenant_a.ctx, theirs.id, "Ahmed", operation_id=uuid7())
        # And straight at the database: the composite foreign key refuses it.
        db.rollback()
        with pytest.raises(Exception):
            db.execute(
                text(
                    "INSERT INTO customer_alias "
                    "(id, tenant_id, customer_id, alias, normalized, status) "
                    "VALUES (:i, :t, :c, 'x', 'x', 'ACTIVE')"
                ),
                {"i": uuid7(), "t": tenant_a.ctx.tenant_id, "c": theirs.id},
            )
        db.rollback()


# --- 10. HTTP surface -------------------------------------------------------


class TestHttpSurface:
    def test_search_route_returns_matches(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, tenant_a.ctx, customer, "Ahmed bhai")
        resp = client.post(
            "/api/v1/search/customers",
            json={"query_text": "ahmed bhai"},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [i["customer_id"] for i in body["items"]] == [str(customer.id)]
        assert body["items"][0]["matched_on"] == "ALIAS"
        assert body["items"][0]["aliases"] == ["Ahmed bhai"]

    def test_search_route_refuses_an_unknown_field(self, client, tenant_a):
        resp = client.post(
            "/api/v1/search/customers",
            json={"query_text": "x", "order_by": "id"},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION"

    def test_resolve_route(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        resp = client.post(
            "/api/v1/search/customers/resolve",
            json={"reference": "Ahmed Khan"},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "RESOLVED"
        assert body["customer"]["customer_id"] == str(customer.id)

    def test_resolve_route_is_ambiguous_for_two_ahmeds(self, client, db, tenant_a):
        make_customer(db, tenant_a.ctx, "C-1", "Ahmed Khan")
        make_customer(db, tenant_a.ctx, "C-2", "Ahmed Ali")
        resp = client.post(
            "/api/v1/search/customers/resolve",
            json={"reference": "Ahmed"},
            headers=tenant_a.auth,
        )
        body = resp.json()
        assert body["status"] == "AMBIGUOUS"
        assert body["customer"] is None
        assert len(body["candidates"]) == 2

    def test_alias_routes_round_trip(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")

        created = client.post(
            f"/api/v1/customers/{customer.id}/aliases",
            json={"operation_id": str(uuid7()), "alias": "Ahmd bhai"},
            headers=tenant_a.auth,
        )
        assert created.status_code == 201, created.text
        alias_id = created.json()["entity"]["id"]

        corrected = client.patch(
            f"/api/v1/customers/{customer.id}/aliases/{alias_id}",
            json={"operation_id": str(uuid7()), "alias": "Ahmed bhai"},
            headers=tenant_a.auth,
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["entity"]["alias"] == "Ahmed bhai"

        listed = client.get(
            f"/api/v1/customers/{customer.id}/aliases", headers=tenant_a.auth
        )
        assert [a["alias"] for a in listed.json()["items"]] == ["Ahmed bhai"]

        retired = client.post(
            f"/api/v1/customers/{customer.id}/aliases/{alias_id}/deactivate",
            json={"operation_id": str(uuid7()), "reason": "no longer used"},
            headers=tenant_a.auth,
        )
        assert retired.status_code == 200, retired.text
        assert (
            client.get(
                f"/api/v1/customers/{customer.id}/aliases", headers=tenant_a.auth
            ).json()["items"]
            == []
        )

    def test_alias_creation_replays_rather_than_duplicating(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        body = {"operation_id": str(uuid7()), "alias": "Ahmed bhai"}
        first = client.post(
            f"/api/v1/customers/{customer.id}/aliases", json=body, headers=tenant_a.auth
        )
        second = client.post(
            f"/api/v1/customers/{customer.id}/aliases", json=body, headers=tenant_a.auth
        )
        assert first.json()["status"] == "APPLIED"
        assert second.json()["status"] == "DUPLICATE"
        assert second.json()["entity"]["id"] == first.json()["entity"]["id"]
        assert (
            len(
                client.get(
                    f"/api/v1/customers/{customer.id}/aliases", headers=tenant_a.auth
                ).json()["items"]
            )
            == 1
        )

    def test_customer_detail_carries_aliases(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, tenant_a.ctx, customer, "Ahmed bhai")
        body = client.get(
            f"/api/v1/customers/{customer.id}", headers=tenant_a.auth
        ).json()
        assert body["aliases"] == ["Ahmed bhai"]

    def test_customer_list_carries_aliases(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        give_alias(db, tenant_a.ctx, customer, "Ahmed bhai")
        body = client.get("/api/v1/customers", headers=tenant_a.auth).json()
        assert body["items"][0]["aliases"] == ["Ahmed bhai"]

    @pytest.mark.parametrize(
        "method,path,payload",
        [
            ("post", "/api/v1/search/customers", {"query_text": "a"}),
            ("post", "/api/v1/search/customers/resolve", {"reference": "a"}),
        ],
    )
    def test_platform_principal_is_refused_on_every_search_route(
        self, client, platform_token, method, path, payload
    ):
        """The platform scope holds no tenant capability, and search is one."""
        resp = getattr(client, method)(
            path, json=payload, headers={"Authorization": f"Bearer {platform_token}"}
        )
        assert resp.status_code in (403, 404), resp.text

    def test_search_route_needs_authentication(self, client):
        resp = client.post("/api/v1/search/customers", json={"query_text": "a"})
        assert resp.status_code == 401

    def test_one_tenants_search_cannot_see_another_over_http(
        self, client, db, tenant_a, tenant_b
    ):
        make_customer(db, tenant_b.ctx, "C-1", "Ahmed Khan")
        body = client.post(
            "/api/v1/search/customers",
            json={"query_text": "Ahmed Khan"},
            headers=tenant_a.auth,
        ).json()
        assert body["items"] == []


# --- 11. the change feed ----------------------------------------------------


class TestFeedIntegration:
    def test_feed_version_moved_so_devices_reseed(self):
        from app.sync.changes import SYNC_FEED_VERSION

        assert SYNC_FEED_VERSION == 3

    def test_aliases_are_not_a_sync_entity_of_their_own(self):
        from app.sync.changes import SYNC_ENTITIES

        assert "customer_alias" not in SYNC_ENTITIES
        assert "row_version" not in CustomerAlias.__table__.columns

    def test_an_alias_write_reaches_the_feed_as_a_customer_change(self, db, tenant_a):
        from app.sync.changes import changes_since

        ctx = tenant_a.ctx
        customer = make_customer(db, ctx, "C-1", "Muhammad Ahmed Khan")
        cursor = changes_since(db, ctx)["cursor"]
        give_alias(db, ctx, customer, "Ahmed bhai")

        page = changes_since(db, ctx, since=cursor)
        customers = [c for c in page["changes"] if c["entity"] == "customer"]
        assert len(customers) == 1
        assert customers[0]["data"]["aliases"] == ["Ahmed bhai"]

    def test_alias_op_types_take_the_commit_order_lock(self):
        from app.sync.serialization import FEED_WRITING_OP_TYPES

        assert {
            "customer.alias.add",
            "customer.alias.update",
            "customer.alias.deactivate",
        } <= FEED_WRITING_OP_TYPES

    def test_no_alias_operation_may_be_queued_offline(self):
        """P0 §7.2: the offline write guarantee is CONFIRM and SKIP alone."""
        from app.sync.envelope import SUPPORTED_OP_TYPES

        assert not any(op.startswith("customer.alias") for op in SUPPORTED_OP_TYPES)

    def test_sync_endpoint_refuses_an_alias_operation(self, client, db, tenant_a):
        customer = make_customer(db, tenant_a.ctx, "C-1", "Muhammad Ahmed Khan")
        resp = client.post(
            "/api/v1/sync/operations",
            json={
                "operations": [
                    {
                        "operation_id": str(uuid7()),
                        "op_type": "customer.alias.add",
                        "payload": {
                            "customer_id": str(customer.id),
                            "alias": "Ahmed bhai",
                        },
                        "client_created_at": "2026-03-15T07:00:00+00:00",
                    }
                ]
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code in (200, 422)
        if resp.status_code == 200:
            assert resp.json()["results"][0]["status"] == "REJECTED"


# --- 12. scale and query count ----------------------------------------------


class TestScale:
    """Reasonable as the book grows, and no N+1 anywhere in the path."""

    @staticmethod
    def _seed(db, ctx, count: int) -> None:
        from app.core.db import next_row_version

        rows = []
        for i in range(count):
            rows.append(
                Customer(
                    tenant_id=ctx.tenant_id,
                    code=f"C-{i:05d}",
                    name=f"Customer Number{i:05d}",
                    normalized_name=normalize_text(f"Customer Number{i:05d}"),
                    default_quantity="1",
                    unit_price_minor=25000,
                    row_version=next_row_version(db),
                )
            )
        db.add_all(rows)
        db.commit()
        db.execute(text("ANALYZE customer"))

    def test_search_over_a_thousand_customers_is_bounded_and_correct(
        self, db, tenant_a
    ):
        ctx = tenant_a.ctx
        self._seed(db, ctx, 1000)
        needle = make_customer(db, ctx, "C-NEEDLE", "Zulfiqar Chaudhry")
        give_alias(db, ctx, needle, "Zulfi bhai")

        started = time.perf_counter()
        result = resolve_customer(db, ctx, "Zulfi bhai")
        elapsed = time.perf_counter() - started

        assert result.status is ResolutionStatus.RESOLVED
        assert result.customer_id == needle.id
        # Generous on purpose: this is a guard against an accidental N+1 or a
        # cross join, not a benchmark of anybody's laptop.
        assert elapsed < 2.0

    @pytest.mark.parametrize("population", [100, 500])
    def test_search_stays_bounded_as_the_book_grows(self, db, tenant_a, population):
        """One identification, however loudly the rest of the book resembles it.

        The seeded names are far more alike than real ones — ``Number00042`` and
        ``Number00004`` share almost every trigram — so trigram similarity
        surfaces the neighbours as candidates. That is the fuzzy source doing its
        job, and it is why nothing fuzzy is ever an identification: exactly one
        customer is *strong*, the rest are marked ``WEAK``, and the page stays
        inside the filter's cap however large the book gets.
        """
        ctx = tenant_a.ctx
        self._seed(db, ctx, population)
        matches = find(db, ctx, "Number00042")

        assert len(matches) <= DEFAULT_SEARCH_LIMIT
        strong = [m for m in matches if m.is_strong]
        assert [m.code for m in strong] == ["C-00042"]
        assert matches[0].code == "C-00042"
        assert all(m.matched_on == MatchKind.FUZZY for m in matches[1:])

        # And with fuzzy off, the loose neighbours are not even candidates.
        exact_only = find(db, ctx, "Number00042", allow_fuzzy=False)
        assert [m.code for m in exact_only] == ["C-00042"]

    def test_search_issues_a_bounded_number_of_statements(self, db, tenant_a):
        """No N+1: the search, plus one batched alias load. Never one per row."""
        ctx = tenant_a.ctx
        for i in range(25):
            customer = make_customer(db, ctx, f"C-{i}", f"Ahmed Number{i}")
            give_alias(db, ctx, customer, f"Ahmed {i} bhai")

        statements: list[str] = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            matches = find(db, ctx, "Ahmed", limit=25)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", record)

        assert len(matches) == 25
        assert all(m.aliases for m in matches)
        assert len(statements) <= 2, statements

    def test_serializing_a_page_of_customers_loads_aliases_once(self, db, tenant_a):
        ctx = tenant_a.ctx
        customers = []
        for i in range(20):
            customer = make_customer(db, ctx, f"C-{i}", f"Person Number{i}")
            give_alias(db, ctx, customer, f"Nick{i}")
            customers.append(customer)

        statements: list[str] = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            payloads = serialize_customers(db, ctx, customers)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", record)

        assert len(payloads) == 20
        assert all(p["aliases"] for p in payloads)
        assert len(statements) == 1, statements


# --- 13. no AI in P8 --------------------------------------------------------


class TestNoInterpreterExists:
    """P8 is deterministic. Nothing here calls a model, and nothing may."""

    def test_no_search_module_imports_an_http_client(self):
        import ast
        import pathlib

        from tests._source import APP_ROOT

        for path in (APP_ROOT / "search").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] not in {
                        "httpx",
                        "requests",
                        "aiohttp",
                        "openai",
                    }, f"{path.name} imports {name}"

    def test_the_interpreter_port_is_still_absent(self):
        from tests._source import APP_ROOT

        assert not (APP_ROOT / "ports" / "ai.py").exists()
        assert not (APP_ROOT / "adapters" / "ai").exists()
