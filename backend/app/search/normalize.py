"""The one normalization path.

Every comparison this product makes between "what a person typed" and "what is
on the books" goes through this module. There is no ``.lower()`` and no
whitespace rule anywhere else in the search code, because two normalizations
that drift apart produce a search that finds a customer on one screen and not on
another — and a resolver that identifies the wrong person is worse than one that
finds nobody.

**Normalization never rewrites stored data for display.** ``normalized_name`` and
``customer_alias.normalized`` sit *beside* the real name and the real alias; the
person's name is shown exactly as it was entered, always. What is normalized is
only ever the comparison key.

What it does, in order:

1. NFKD decompose and drop combining marks, so ``Ayesha`` matches ``Áyesha`` and
   an Urdu word matches the same word written with harakat;
2. NFKC recompose, so compatibility forms (full-width digits, ligatures) fold to
   their ordinary equivalents;
3. casefold — stronger than ``lower()`` and defined for non-Latin scripts;
4. replace every non-alphanumeric character with a space, so ``Ahmed-bhai``,
   ``Ahmed_bhai`` and ``Ahmed  bhai`` all become one comparison key;
5. collapse runs of whitespace and strip.

``str.isalnum`` is true for Urdu, Arabic and Devanagari letters, so a name stored
in its own script normalizes to itself and matches when typed in that script.

**What it deliberately does not do.** There is no transliteration between scripts
and no Roman-Urdu spelling model: ``احمد`` does not match ``Ahmed`` unless one of
them is stored as an alias of the customer. That is the whole point of aliases —
a person records the name they actually use, and the system stops guessing. A
transliteration engine would need language tooling, would be wrong for names in
ways nobody could predict, and would make identification less trustworthy rather
than more.
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "MAX_QUERY_LENGTH",
    "PHONE_SUFFIX_DIGITS",
    "PHONE_SUFFIX_MIN_DIGITS",
    "looks_like_phone",
    "normalize_phone",
    "normalize_text",
    "normalize_tokens",
    "phone_suffix",
]

#: Longer than any name on the books, and short enough that no query can be used
#: to make the database do unbounded work. Queries are truncated, never rejected:
#: a paste accident should find nothing useful, not raise.
MAX_QUERY_LENGTH = 120

#: The shortest digit string that may be matched as a phone *suffix*. Below this
#: a "phone" match would be a coincidence — three digits match half the book.
PHONE_SUFFIX_MIN_DIGITS = 7

#: How many trailing digits a suffix match compares.
#:
#: Numbers are stored E.164 (``+923001234567``) and people type them however
#: they hold them — often in the national form (``0300-1234567``), where a
#: leading ``0`` stands in for the country code. Comparing the *trailing* nine
#: digits makes both forms find the same person without this module knowing a
#: single thing about any country's dialling plan. Nine digits is specific enough
#: that a collision would have to be arranged on purpose.
PHONE_SUFFIX_DIGITS = 9


def normalize_text(value: str | None) -> str:
    """The comparison key for a name, an alias, a code or an area."""
    if not value:
        return ""
    text = value[:MAX_QUERY_LENGTH]
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = unicodedata.normalize("NFKC", stripped).casefold()
    spaced = "".join(ch if ch.isalnum() else " " for ch in folded)
    return " ".join(spaced.split())


def normalize_tokens(value: str | None) -> tuple[str, ...]:
    """The normalized words of ``value``, in the order they were written.

    Order is preserved but never *required*: token matching compares sets, which
    is what makes "Ahmed bhai" and "bhai Ahmed" the same query.
    """
    normalized = normalize_text(value)
    return tuple(normalized.split()) if normalized else ()


def normalize_phone(value: str | None) -> str:
    """Every digit in ``value``, in order, with nothing else.

    ``+92 300 123-4567``, ``0092-3001234567`` and ``+923001234567`` all reduce to
    a digit string, so a person may type a number however they hold it. Non-ASCII
    digits (Eastern Arabic, Devanagari) fold to their ASCII value rather than
    being discarded, because a phone number typed in Urdu digits is still that
    phone number.
    """
    if not value:
        return ""
    digits: list[str] = []
    for ch in value[:MAX_QUERY_LENGTH]:
        if ch.isdigit():
            try:
                digits.append(str(unicodedata.digit(ch)))
            except (TypeError, ValueError):  # pragma: no cover - isdigit implies digit
                continue
    return "".join(digits)


def phone_suffix(value: str | None) -> str:
    """The trailing digits a suffix comparison uses. Empty when too short."""
    digits = normalize_phone(value)
    if len(digits) < PHONE_SUFFIX_MIN_DIGITS:
        return ""
    return digits[-PHONE_SUFFIX_DIGITS:]


def looks_like_phone(value: str | None) -> bool:
    """True when ``value`` is plausibly a phone number rather than a name.

    Deliberately strict: any letter at all means it is a name. ``+92 300`` is a
    phone fragment; ``Ahmed 3`` is not.
    """
    if not value:
        return False
    if any(ch.isalpha() for ch in value):
        return False
    return len(normalize_phone(value)) >= PHONE_SUFFIX_MIN_DIGITS
