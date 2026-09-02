/**
 * Money *display* only.
 *
 * P0 §5: the server is the sole authority on every amount. Nothing here adds,
 * multiplies, or otherwise derives money — it takes an integer count of minor
 * units the server already computed and renders it. If a screen needs a total,
 * the total comes from the API.
 *
 * The split is done on the integer, not by dividing, so no float is involved.
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
