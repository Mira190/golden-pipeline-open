import { describe, expect, it } from "vitest";
import { FLAT_SHIPPING_CENTS, type Line, PricingError, quote } from "./pricing.js";

const line = (unitCents = 1_000, quantity = 1, sku = "A"): Line => ({ sku, unitCents, quantity });

describe("quote", () => {
  it("charges flat shipping on small orders", () => {
    expect(quote([line(1_000, 2)])).toEqual({
      subtotal: 2_000,
      discount: 0,
      shipping: FLAT_SHIPPING_CENTS,
      total: 2_800,
    });
  });

  it("ships free from the threshold, judged after discount", () => {
    expect(quote([line(10_000)]).shipping).toBe(0);
    expect(quote([line(9_999)]).shipping).toBe(FLAT_SHIPPING_CENTS);
    expect(quote([line(11_000)], 10)).toEqual({
      subtotal: 11_000,
      discount: 1_100,
      shipping: FLAT_SHIPPING_CENTS,
      total: 10_700,
    });
  });

  it("rounds the discount down", () => {
    expect(quote([line(15)], 10).discount).toBe(1);
  });

  it("sums every line", () => {
    expect(quote([line(100, 3), line(50, 2, "B")]).subtotal).toBe(400);
  });

  it.each([0, 50])("accepts discount %i", (percent) => {
    expect(() => quote([line()], percent)).not.toThrow();
  });

  it.each([
    [[], 0, "an order needs at least one line"],
    [[line()], -1, "discount must be between 0 and 50 percent"],
    [[line()], 51, "discount must be between 0 and 50 percent"],
    [[line(1, 1, "")], 0, "sku must not be empty"],
    [[line(-1)], 0, "unit price must be a non-negative integer"],
    [[line(1.5)], 0, "unit price must be a non-negative integer"],
    [[line(1, 0)], 0, "quantity must be a positive integer"],
    [[line(1, 1.5)], 0, "quantity must be a positive integer"],
  ] as const)("rejects invalid input %#", (lines, percent, message) => {
    expect(() => quote([...lines], percent)).toThrow(new PricingError(message));
  });

  it("accepts zero-priced lines", () => {
    expect(quote([line(0)]).subtotal).toBe(0);
  });
});
