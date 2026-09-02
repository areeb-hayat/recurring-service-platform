/**
 * Exact fixed-point arithmetic for quantities.
 *
 * The backend stores quantity as NUMERIC(12,3) and accepts it on the wire as a
 * *string* precisely so a JSON float never touches it. If the client parsed that
 * string into a JS `number` to run a +/- stepper, it would reintroduce the exact
 * representation error the string encoding exists to remove: `0.1 + 0.2` is not
 * `0.3` in binary floating point.
 *
 * So the stepper works on scaled integers (units of 0.001) and formats back to a
 * string. No `parseFloat`, no `toFixed`, nowhere in the quantity path.
 *
 * This is quantity only. Money is never computed on the client at all (P0 §5):
 * `src/lib/money.ts` formats server-supplied minor units for display and does
 * no arithmetic.
 */

export const QUANTITY_SCALE = 3;
const SCALE_FACTOR = 1000n; // 10 ** QUANTITY_SCALE
/** NUMERIC(12,3) — the largest value the column can hold. */
const MAX_SCALED = 999_999_999_999n;

export class QuantityFormatError extends Error {}

/** Parse a decimal string into thousandths. Rejects anything the column cannot store. */
export function parseQuantity(text: string): bigint {
  const trimmed = text.trim();
  if (!/^\d+(\.\d{1,3})?$/.test(trimmed)) {
    throw new QuantityFormatError(
      "Enter a number with up to 3 decimal places, for example 2 or 1.5",
    );
  }
  const [whole = "0", fraction = ""] = trimmed.split(".");
  const scaled = BigInt(whole) * SCALE_FACTOR + BigInt(fraction.padEnd(QUANTITY_SCALE, "0"));
  if (scaled > MAX_SCALED) throw new QuantityFormatError("That quantity is too large");
  return scaled;
}

/** Format thousandths back to the wire/display string, without trailing zeros. */
export function formatQuantity(scaled: bigint): string {
  const clamped = scaled < 0n ? 0n : scaled;
  const whole = clamped / SCALE_FACTOR;
  const fraction = (clamped % SCALE_FACTOR).toString().padStart(QUANTITY_SCALE, "0");
  const trimmed = fraction.replace(/0+$/, "");
  return trimmed ? `${whole}.${trimmed}` : whole.toString();
}

/** Step a quantity string by whole units, never below zero and never above the column. */
export function stepQuantity(current: string, deltaUnits: number): string {
  let scaled: bigint;
  try {
    scaled = parseQuantity(current);
  } catch {
    scaled = 0n; // stepping out of an unparseable draft starts from zero
  }
  const next = scaled + BigInt(deltaUnits) * SCALE_FACTOR;
  if (next < 0n) return "0";
  if (next > MAX_SCALED) return formatQuantity(MAX_SCALED);
  return formatQuantity(next);
}

/** True when the string is a quantity the backend will accept. */
export function isValidQuantity(text: string): boolean {
  try {
    parseQuantity(text);
    return true;
  } catch {
    return false;
  }
}

export function isZeroQuantity(text: string): boolean {
  try {
    return parseQuantity(text) === 0n;
  } catch {
    return false;
  }
}
