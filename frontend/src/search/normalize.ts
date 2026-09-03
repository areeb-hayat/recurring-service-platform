/**
 * The client's single normalization path — and an honest account of its limits.
 *
 * This is a *mirror* of `app/search/normalize.py`, and it exists for exactly one
 * reason: offline, there is no server to ask, and the device still has to be
 * able to find "Ahmed bhai" in the customers it has already synchronised. It is
 * used by nothing else. Every online search and every identification goes to the
 * server, where the authoritative implementation lives.
 *
 * It performs the same five steps, in the same order:
 *
 *  1. NFKD decompose and drop combining marks, so `Ayesha` matches `Áyesha`;
 *  2. NFKC recompose, folding compatibility forms;
 *  3. lower-case;
 *  4. replace every non-letter, non-digit with a space, so `Ahmed-bhai`,
 *     `Ahmed_bhai` and `Ahmed  bhai` become one comparison key;
 *  5. collapse whitespace and trim.
 *
 * **Where it differs from the server, and why that is safe.** Python's
 * `str.casefold()` is stronger than JavaScript's `toLowerCase()` — German `ß`
 * folds to `ss` there and stays `ß` here — so a handful of exotic spellings can
 * normalize differently on the two sides. That divergence can only ever make
 * *offline* search miss a row the server would have found; it can never make it
 * find the wrong person, because the answer offline is always a list somebody
 * picks from. Online, this function is not consulted at all. If that trade ever
 * stops being acceptable the fix is a full case-folding table here, not a second
 * matching implementation.
 *
 * **Display text is never rewritten.** Everything below produces a comparison
 * key. The name on screen is always exactly what the owner typed.
 */

/** Longer than any name on the books; the same bound the server applies. */
export const MAX_QUERY_LENGTH = 120;

/** The shortest digit string that may be matched as a phone suffix. */
export const PHONE_SUFFIX_MIN_DIGITS = 7;

/** How many trailing digits a suffix match compares. */
export const PHONE_SUFFIX_DIGITS = 9;

const COMBINING = /\p{M}/gu;
const NOT_ALPHANUMERIC = /[^\p{L}\p{N}]+/gu;

/** The comparison key for a name, an alias, a code or an area. */
export function normalizeText(value: string | null | undefined): string {
  if (!value) return "";
  return value
    .slice(0, MAX_QUERY_LENGTH)
    .normalize("NFKD")
    .replace(COMBINING, "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(NOT_ALPHANUMERIC, " ")
    .trim()
    .replace(/\s+/g, " ");
}

/**
 * The normalized words of `value`.
 *
 * Order is preserved but never required: token matching compares memberships,
 * which is what makes "Ahmed bhai" and "bhai Ahmed" the same query.
 */
export function normalizeTokens(value: string | null | undefined): string[] {
  const normalized = normalizeText(value);
  return normalized ? normalized.split(" ") : [];
}

/**
 * Every digit in `value`, in order, with nothing else.
 *
 * `+92 300 123-4567` and `0092-3001234567` both reduce to a digit string, so a
 * number may be typed however it is held.
 */
export function normalizePhone(value: string | null | undefined): string {
  if (!value) return "";
  let digits = "";
  for (const ch of value.slice(0, MAX_QUERY_LENGTH)) {
    // Unicode-aware: an Eastern Arabic or Devanagari digit is still that digit.
    const decimal = ch.normalize("NFKD").replace(COMBINING, "");
    if (decimal.length === 1 && decimal >= "0" && decimal <= "9") digits += decimal;
  }
  return digits;
}

/** The trailing digits a suffix comparison uses. Empty when too short. */
export function phoneSuffix(value: string | null | undefined): string {
  const digits = normalizePhone(value);
  if (digits.length < PHONE_SUFFIX_MIN_DIGITS) return "";
  return digits.slice(-PHONE_SUFFIX_DIGITS);
}

/**
 * True when `value` is plausibly a phone number rather than a name.
 *
 * Strict on purpose: any letter at all means it is a name.
 */
export function looksLikePhone(value: string | null | undefined): boolean {
  if (!value) return false;
  if (/\p{L}/u.test(value)) return false;
  return normalizePhone(value).length >= PHONE_SUFFIX_MIN_DIGITS;
}
