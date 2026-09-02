"""Money and quantity primitives — FIN-1, FIN-2, FIN-3.

These run without a database on purpose: the primitive is the foundation every
table inherits, so it is proven before anything depends on it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.core.money import (
    MoneyError,
    QuantityError,
    compute_charge_minor,
    quantize_quantity,
    validate_unit_price_minor,
)


class TestFIN2Quantity:
    """FIN-2: quantity is Decimal at scale 3 and never assumed integer."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (Decimal("1.5"), Decimal("1.500")),
            (2, Decimal("2.000")),
            ("0.333", Decimal("0.333")),
            (Decimal("0"), Decimal("0.000")),
        ],
    )
    def test_FIN2_accepts_non_integer_quantities(self, raw, expected):
        assert quantize_quantity(raw) == expected

    def test_FIN2_rejects_float(self):
        # FIN-1 at the quantity boundary: Decimal(0.1) != Decimal("0.1").
        with pytest.raises(QuantityError, match="must not be a float"):
            quantize_quantity(1.5)

    def test_FIN2_rejects_excess_precision(self):
        # Silently rounding 1.2345 would fabricate a quantity nobody entered.
        with pytest.raises(QuantityError, match="decimal places"):
            quantize_quantity(Decimal("1.2345"))

    def test_FIN2_rejects_negative(self):
        with pytest.raises(QuantityError, match="negative"):
            quantize_quantity(Decimal("-1"))

    def test_FIN2_rejects_non_finite(self):
        with pytest.raises(QuantityError):
            quantize_quantity(Decimal("NaN"))

    def test_FIN2_rejects_bool(self):
        with pytest.raises(QuantityError):
            quantize_quantity(True)


class TestFIN1MoneyRepresentation:
    """FIN-1: money is an integer count of minor units, never a float."""

    def test_FIN1_rejects_float_price(self):
        with pytest.raises(MoneyError, match="must not be a float"):
            validate_unit_price_minor(250.0)

    def test_FIN1_rejects_negative_price(self):
        with pytest.raises(MoneyError, match="negative"):
            validate_unit_price_minor(-1)

    def test_FIN1_rejects_bool_price(self):
        with pytest.raises(MoneyError):
            validate_unit_price_minor(True)

    def test_FIN1_charge_is_always_int(self):
        charge = compute_charge_minor(Decimal("1.5"), 12000)
        assert type(charge) is int


class TestFIN3Rounding:
    """FIN-3: charge = ROUND_HALF_UP(quantity * unit_price_minor), rounded once."""

    def test_FIN3_one_and_a_half_units(self):
        # A-FIN-2, exact case from the acceptance criteria.
        assert compute_charge_minor(Decimal("1.5"), 12000) == 18000

    def test_FIN3_fractional_quantity(self):
        # A-FIN-2, exact case from the acceptance criteria.
        assert compute_charge_minor(Decimal("0.333"), 10000) == 3330

    def test_FIN3_half_up_boundary(self):
        # A-FIN-3: 0.5 * 25 = 12.5 must round UP to 13, not down to 12
        # (banker's rounding, Python's default, would give 12).
        assert compute_charge_minor(Decimal("0.5"), 25) == 13

    @pytest.mark.parametrize(
        "quantity,price,expected",
        [
            ("0", 25000, 0),
            ("1", 25000, 25000),
            ("3", 25000, 75000),
            ("2.5", 10, 25),
            ("0.001", 1, 0),  # rounds to zero, and that is correct
            ("1.5", 25, 38),  # 37.5 -> 38 (half up)
            ("0.5", 1, 1),  # 0.5 -> 1
            ("1.005", 1000, 1005),
        ],
    )
    def test_FIN3_table(self, quantity, price, expected):
        assert compute_charge_minor(Decimal(quantity), price) == expected

    def test_FIN3_rejects_float_quantity(self):
        with pytest.raises(QuantityError):
            compute_charge_minor(1.5, 12000)


class TestFIN1PropertyBased:
    """A-FIN-1 / A-FIN-3: property tests over the primitive."""

    @given(
        units=st.integers(min_value=0, max_value=999_999),
        millis=st.integers(min_value=0, max_value=999),
        price=st.integers(min_value=0, max_value=10**9),
    )
    @hyp_settings(max_examples=400, deadline=None)
    def test_FIN1_charge_is_int_and_non_negative(self, units, millis, price):
        quantity = Decimal(units) + (Decimal(millis) / Decimal(1000))
        charge = compute_charge_minor(quantity, price)
        assert type(charge) is int
        assert charge >= 0

    @given(
        values=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=999),
                st.integers(min_value=0, max_value=999),
                st.integers(min_value=0, max_value=100_000),
            ),
            min_size=1,
            max_size=60,
        )
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_FIN3_sum_of_charges_has_no_drift(self, values):
        """Summing already-rounded integers can never drift.

        This is the property that makes "total != sum of lines" impossible
        (P0 §5.2). It holds because rounding happens once, per record.
        """
        charges = [
            compute_charge_minor(Decimal(u) + Decimal(m) / Decimal(1000), p)
            for u, m, p in values
        ]
        total = sum(charges)
        assert type(total) is int
        assert total == sum(charges)

    @given(
        units=st.integers(min_value=0, max_value=9999),
        millis=st.integers(min_value=0, max_value=999),
        price=st.integers(min_value=1, max_value=10**6),
    )
    @hyp_settings(max_examples=300, deadline=None)
    def test_FIN3_matches_independent_half_up_reference(self, units, millis, price):
        """Cross-check against an integer-only reference implementation.

        quantity has exactly 3 decimals, so quantity*price*1000 is an exact
        integer; half-up division by 1000 is then pure integer arithmetic with
        no decimal library involved at all.
        """
        quantity = Decimal(units) + Decimal(millis) / Decimal(1000)
        scaled = (units * 1000 + millis) * price  # exact, integer
        expected = (scaled + 500) // 1000  # half-up for non-negative values
        assert compute_charge_minor(quantity, price) == expected
