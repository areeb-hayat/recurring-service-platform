"""Money and quantity primitives.

The frozen financial representation (P0 §5.1, §5.2):

* Money is an integer count of **minor units** (paisa for PKR). Python ``int``,
  PostgreSQL ``BIGINT``. Never ``float``, never ``Decimal`` in storage.
* Quantity is ``Decimal`` at scale 3, PostgreSQL ``NUMERIC(12,3)``. Never assumed
  to be an integer.
* ``charge_minor = round_half_up(quantity * unit_price_minor)`` — rounded exactly
  once, here, at the daily service record. Nothing downstream re-rounds.
* ``commission_minor = round_half_up(base_amount_minor * rate_bp / 10000)`` — the
  same half-up rule at the commission-event level (P0 §5.2, COM-9). It shares
  :func:`round_half_up` with the charge rule rather than repeating it, so there
  is exactly one rounding implementation in the system.

Invariants: FIN-1, FIN-2, FIN-3, COM-9.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

__all__ = [
    "QUANTITY_SCALE",
    "QUANTITY_MAX",
    "RATE_BP_SCALE",
    "MoneyError",
    "QuantityError",
    "quantize_quantity",
    "compute_charge_minor",
    "validate_rate_bp",
    "apply_rate_bp",
    "round_half_up",
    "multiply_minor",
    "format_minor",
]

QUANTITY_SCALE = 3
_QUANTITY_EXPONENT = Decimal(1).scaleb(-QUANTITY_SCALE)  # Decimal('0.001')

# NUMERIC(12,3) => 12 significant digits total, 3 after the point.
QUANTITY_MAX = Decimal("999999999.999")

# Guards against absurd configuration rather than expressing a business limit.
# Comfortably inside BIGINT once multiplied by QUANTITY_MAX.
_UNIT_PRICE_MINOR_MAX = 10**12

# Basis points: 10000 bp = 100%. An integer scale, so a commission rate never
# needs a decimal and can never be a float (COM-9, P0 §6).
RATE_BP_SCALE = 10000


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


def round_half_up(value: Decimal) -> int:
    """The one rounding implementation in the system (P0 §5.2).

    Half-up and symmetric about zero: ``Decimal.quantize(ROUND_HALF_UP)`` rounds
    away from zero, so ``-0.5`` becomes ``-1`` exactly as ``0.5`` becomes ``1``.
    That symmetry matters for commission, where a correction produces a negative
    base and must reverse the same magnitude it earned.

    An explicit high-precision context so the result never depends on ambient
    decimal settings.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        rounded = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    result = int(rounded)
    assert isinstance(result, int)  # FIN-1: this boundary returns an int, always.
    return result


def multiply_minor(quantity: Decimal, unit_price_minor: int) -> int:
    """``round_half_up(quantity * unit_price_minor)`` for a *non-billing* amount.

    P6's operating-cost estimator needs the identical arithmetic
    :func:`compute_charge_minor` performs, but over a usage quantity that is not
    a service quantity: audio hours, GB-months and token counts are not
    ``NUMERIC(12,3)`` and are not what FIN-2 constrains. Rather than relax
    :func:`quantize_quantity` — which exists to keep *customer billing* exact —
    this exposes the same single rounding rule for the other caller.

    The FIN-3 "one rounding point" rule is untouched: it is a statement about the
    customer ledger, and nothing here ever posts to it.

    >>> multiply_minor(Decimal("20.833333"), 22)
    458
    """
    _reject_float(quantity, QuantityError, "quantity")
    price = validate_unit_price_minor(unit_price_minor)
    if not isinstance(quantity, Decimal):
        raise QuantityError("quantity must be a Decimal")
    if not quantity.is_finite() or quantity < 0:
        raise QuantityError(f"quantity must be finite and non-negative (got {quantity})")

    with localcontext() as ctx:
        ctx.prec = 50
        product = quantity * Decimal(price)
    return round_half_up(product)


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

    # 12 digits of quantity x 13 of price stays far inside the 50-digit context.
    with localcontext() as ctx:
        ctx.prec = 50
        product = qty * Decimal(price)
    return round_half_up(product)


def validate_rate_bp(value: int) -> int:
    """COM-9: a commission rate is an integer 0..10000 basis points."""
    _reject_float(value, MoneyError, "rate_bp")
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"rate_bp must be an int in basis points (got {type(value).__name__})"
        )
    if not 0 <= value <= RATE_BP_SCALE:
        raise MoneyError(f"rate_bp must be between 0 and {RATE_BP_SCALE} (got {value})")
    return value


def apply_rate_bp(base_amount_minor: int, rate_bp: int) -> int:
    """``round_half_up(base_amount_minor * rate_bp / 10000)`` (COM-9, P0 §5.2).

    The commission-level use of the *same* rounding rule as the charge — hence
    the shared :func:`round_half_up` — expressed in integer basis points so no
    decimal rate ever exists to be stored as a float.

    ``base_amount_minor`` may be negative: a correction reverses commission on
    the difference, and the half-up rule is symmetric about zero.

    >>> apply_rate_bp(100000, 250)
    2500
    >>> apply_rate_bp(-30000, 250)
    -750
    """
    _reject_float(base_amount_minor, MoneyError, "base_amount_minor")
    if isinstance(base_amount_minor, bool) or not isinstance(base_amount_minor, int):
        raise MoneyError(
            "base_amount_minor must be an int in minor units "
            f"(got {type(base_amount_minor).__name__})"
        )
    rate = validate_rate_bp(rate_bp)

    with localcontext() as ctx:
        ctx.prec = 50
        product = Decimal(base_amount_minor) * Decimal(rate) / Decimal(RATE_BP_SCALE)
    return round_half_up(product)


def format_minor(amount_minor: int, currency: str, currency_exponent: int) -> str:
    """Render an amount for a *human being to read*, never for a machine to parse.

    REM-7 is why this exists on the server. A reminder hands its delivery
    provider a finished string — "PKR 3,000.00" — and never the integer, the
    exponent or a formula, so no provider can compute, re-round or misinterpret
    an amount. The provider delivers; it does not do arithmetic.

    Deliberately locale-free: grouping by three with a full stop for the
    fractional part, which is what the tenant's own currency configuration
    already implies and what a phone message needs. Nothing parses this back.

    >>> format_minor(300000, "PKR", 2)
    'PKR 3,000.00'
    >>> format_minor(0, "PKR", 2)
    'PKR 0.00'
    >>> format_minor(-4500, "USD", 2)
    'USD -45.00'
    >>> format_minor(120, "JPY", 0)
    'JPY 120'
    """
    _reject_float(amount_minor, MoneyError, "amount_minor")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise MoneyError(
            f"amount_minor must be an int in minor units (got {type(amount_minor).__name__})"
        )
    if not 0 <= currency_exponent <= 4:
        raise MoneyError(f"currency_exponent must be 0..4 (got {currency_exponent})")

    with localcontext() as ctx:
        ctx.prec = 50
        major = Decimal(amount_minor).scaleb(-currency_exponent)
    sign = "-" if major < 0 else ""
    digits = f"{abs(major):.{currency_exponent}f}"
    whole, _, fraction = digits.partition(".")
    grouped = f"{int(whole):,}"
    number = f"{grouped}.{fraction}" if fraction else grouped
    return f"{currency} {sign}{number}"
