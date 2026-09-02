"""Money and quantity primitives.

The frozen financial representation (P0 §5.1, §5.2):

* Money is an integer count of **minor units** (paisa for PKR). Python ``int``,
  PostgreSQL ``BIGINT``. Never ``float``, never ``Decimal`` in storage.
* Quantity is ``Decimal`` at scale 3, PostgreSQL ``NUMERIC(12,3)``. Never assumed
  to be an integer.
* ``charge_minor = round_half_up(quantity * unit_price_minor)`` — rounded exactly
  once, here, at the daily service record. Nothing downstream re-rounds.

Invariants: FIN-1, FIN-2, FIN-3.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

__all__ = [
    "QUANTITY_SCALE",
    "QUANTITY_MAX",
    "MoneyError",
    "QuantityError",
    "quantize_quantity",
    "compute_charge_minor",
]

QUANTITY_SCALE = 3
_QUANTITY_EXPONENT = Decimal(1).scaleb(-QUANTITY_SCALE)  # Decimal('0.001')

# NUMERIC(12,3) => 12 significant digits total, 3 after the point.
QUANTITY_MAX = Decimal("999999999.999")

# Guards against absurd configuration rather than expressing a business limit.
# Comfortably inside BIGINT once multiplied by QUANTITY_MAX.
_UNIT_PRICE_MINOR_MAX = 10**12


class MoneyError(ValueError):
    """Raised when a money value violates the frozen representation."""


class QuantityError(ValueError):
    """Raised when a quantity value violates the frozen representation."""


def _reject_float(value: object, error: type[ValueError], field: str) -> None:
    """FIN-1: binary floating point never touches money or quantity.

    Rejected at the boundary rather than silently coerced, because
    ``Decimal(0.1)`` is not ``Decimal("0.1")`` and the difference is exactly the
    class of bug the integer-minor-unit rule exists to remove.
    """
    if isinstance(value, float):
        raise error(
            f"{field} must not be a float; pass an int, str or Decimal "
            f"(got float {value!r})"
        )


def quantize_quantity(value: Decimal | int | str) -> Decimal:
    """Normalise a quantity to exactly ``NUMERIC(12,3)``.

    Accepts ``Decimal``, ``int`` or ``str``. Rejects ``float`` (FIN-1), negatives,
    non-finite values, values above :data:`QUANTITY_MAX`, and values carrying more
    precision than the column can store — a quantity of ``1.2345`` is a caller
    bug, not something to round away silently.
    """
    _reject_float(value, QuantityError, "quantity")

    if isinstance(value, bool):  # bool is an int subclass; never a quantity.
        raise QuantityError("quantity must not be a bool")

    try:
        quantity = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise QuantityError(f"quantity is not a valid decimal: {value!r}") from exc

    if not quantity.is_finite():
        raise QuantityError(f"quantity must be finite (got {value!r})")
    if quantity < 0:
        raise QuantityError(f"quantity must not be negative (got {quantity})")
    if quantity > QUANTITY_MAX:
        raise QuantityError(f"quantity exceeds NUMERIC(12,3) range (got {quantity})")

    quantized = quantity.quantize(_QUANTITY_EXPONENT, rounding=ROUND_HALF_UP)
    if quantized != quantity:
        raise QuantityError(
            f"quantity carries more than {QUANTITY_SCALE} decimal places "
            f"(got {quantity}); round it before recording"
        )
    return quantized


def validate_unit_price_minor(value: int) -> int:
    """Validate a unit price expressed in minor units."""
    _reject_float(value, MoneyError, "unit_price_minor")
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"unit_price_minor must be an int in minor units (got {type(value).__name__})"
        )
    if value < 0:
        raise MoneyError(f"unit_price_minor must not be negative (got {value})")
    if value > _UNIT_PRICE_MINOR_MAX:
        raise MoneyError(f"unit_price_minor is implausibly large (got {value})")
    return value


def compute_charge_minor(quantity: Decimal | int | str, unit_price_minor: int) -> int:
    """The single rounding point in the system (FIN-3).

    ``charge_minor = ROUND_HALF_UP(quantity * unit_price_minor)``

    Returns a plain ``int`` of minor units. Statements, dashboards and balances
    sum these already-rounded integers and never re-round, which is what makes
    "total != sum of lines" impossible by construction (P0 §5.2).

    >>> compute_charge_minor(Decimal("1.5"), 12000)
    18000
    >>> compute_charge_minor(Decimal("0.333"), 10000)
    3330
    >>> compute_charge_minor(Decimal("0.5"), 25)
    13
    """
    qty = quantize_quantity(quantity)
    price = validate_unit_price_minor(unit_price_minor)

    # An explicit high-precision context so the result never depends on ambient
    # decimal settings. 12 digits of quantity x 13 of price stays far inside 50.
    with localcontext() as ctx:
        ctx.prec = 50
        product = qty * Decimal(price)
        charge = product.quantize(Decimal(1), rounding=ROUND_HALF_UP)

    result = int(charge)
    assert isinstance(result, int)  # FIN-1: the boundary returns an int, always.
    return result
