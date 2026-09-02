import { describe, expect, it } from "vitest";

import {
  formatQuantity,
  isValidQuantity,
  isZeroQuantity,
  parseQuantity,
  stepQuantity,
} from "./decimal";
import { formatMoney } from "./money";
import { uuidv7 } from "./uuid";

describe("quantity arithmetic", () => {
  it("parses whole and decimal quantities exactly", () => {
    expect(parseQuantity("2")).toBe(2000n);
    expect(parseQuantity("1.5")).toBe(1500n);
    expect(parseQuantity("0.333")).toBe(333n);
  });

  it("rejects precision the NUMERIC(12,3) column cannot store", () => {
    expect(isValidQuantity("1.2345")).toBe(false);
    expect(isValidQuantity("-1")).toBe(false);
    expect(isValidQuantity("")).toBe(false);
    expect(isValidQuantity("two")).toBe(false);
  });

  it("does not accumulate binary floating point error", () => {
    // 0.1 + 0.2 !== 0.3 as JS numbers. Ten steps of 0.1 must land exactly on 1.
    let value = "0";
    for (let i = 0; i < 10; i += 1) {
      value = formatQuantity(parseQuantity(value) + 100n);
    }
    expect(value).toBe("1");
  });

  it("steps by whole units and never below zero", () => {
    expect(stepQuantity("2", 1)).toBe("3");
    expect(stepQuantity("1.5", 1)).toBe("2.5");
    expect(stepQuantity("1.5", -1)).toBe("0.5");
    expect(stepQuantity("0.5", -1)).toBe("0");
    expect(stepQuantity("0", -1)).toBe("0");
  });

  it("formats without trailing zeros", () => {
    expect(formatQuantity(2000n)).toBe("2");
    expect(formatQuantity(1500n)).toBe("1.5");
    expect(formatQuantity(333n)).toBe("0.333");
  });

  it("recognises a zero quantity", () => {
    expect(isZeroQuantity("0")).toBe(true);
    expect(isZeroQuantity("0.000")).toBe(true);
    expect(isZeroQuantity("0.001")).toBe(false);
  });
});

describe("money display", () => {
  it("splits minor units without dividing", () => {
    expect(formatMoney(50000, "PKR", 2)).toBe("PKR 500.00");
    expect(formatMoney(5, "PKR", 2)).toBe("PKR 0.05");
    expect(formatMoney(-2500, "PKR", 2)).toBe("-PKR 25.00");
    expect(formatMoney(1234567, "PKR", 2)).toBe("PKR 12,345.67");
  });

  it("handles a zero-exponent currency", () => {
    expect(formatMoney(1500, "JPY", 0)).toBe("JPY 1,500");
  });
});

describe("uuidv7", () => {
  it("produces a well-formed version 7 uuid", () => {
    const id = uuidv7();
    expect(id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("is unique across calls", () => {
    const ids = new Set(Array.from({ length: 500 }, () => uuidv7()));
    expect(ids.size).toBe(500);
  });
});
