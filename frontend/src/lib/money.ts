/**
 * Money *display* only.
 *
 * P0 §5: the server is the sole authority on every amount. Nothing here adds,
 * multiplies, or otherwise derives money — it takes an integer count of minor
 * units the server already computed and renders it. If a screen needs a total,
 * the total comes from the API.
 *
 * The split is done on the integer, not by dividing, so no float is involved.
 *
 * The one thing here that goes the other way is {@link majorToMinor}, which
 * turns what a person typed into the integer the API takes. It is parsing, not
 * arithmetic: the digits are concatenated and padded, never multiplied by a
 * power of ten in floating point.
 */

export function formatMoney(
  minor: number,
  currency: string,
  exponent: number,
): string {
  const negative = minor < 0;
  const digits = Math.abs(minor).toString().padStart(exponent + 1, "0");
  const whole = exponent === 0 ? digits : digits.slice(0, digits.length - exponent);
  const fraction = exponent === 0 ? "" : digits.slice(digits.length - exponent);
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const amount = fraction ? `${grouped}.${fraction}` : grouped;
  return `${negative ? "-" : ""}${currency} ${amount}`;
}


/**
 * "12.50" -> 1250 at exponent 2. `null` when the text is not a valid amount.
 *
 * String surgery rather than `Number(text) * 100`: the multiply is exactly the
 * floating-point step the integer-minor-unit rule exists to remove (`19.99 *
 * 100` is `1998.9999999999998`). Digits are concatenated and the fraction is
 * padded, so the result is exact for every input the pattern accepts.
 */
export function majorToMinor(text: string, exponent: number): number | null {
  const trimmed = text.trim();
  const pattern =
    exponent === 0
      ? /^\d+$/
      : new RegExp("^\\d+(\\.\\d{0," + exponent + "})?$");
  if (!pattern.test(trimmed)) return null;
  const [whole = "0", fraction = ""] = trimmed.split(".");
  return Number(whole + fraction.padEnd(exponent, "0"));
}

/** The inverse, for pre-filling an amount field from a server figure. */
export function minorToMajor(minor: number, exponent: number): string {
  const negative = minor < 0;
  const digits = Math.abs(minor).toString().padStart(exponent + 1, "0");
  const whole = exponent === 0 ? digits : digits.slice(0, digits.length - exponent);
  const fraction = exponent === 0 ? "" : digits.slice(digits.length - exponent);
  return `${negative ? "-" : ""}${fraction ? `${whole}.${fraction}` : whole}`;
}
