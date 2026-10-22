/** Order pricing: pure functions, money in integer cents. */

export const FREE_SHIPPING_CENTS = 10_000;
export const FLAT_SHIPPING_CENTS = 800;
export const MAX_DISCOUNT_PERCENT = 50;

export class PricingError extends Error {}

export interface Line {
  sku: string;
  unitCents: number;
  quantity: number;
}

export interface Quote {
  subtotal: number;
  discount: number;
  shipping: number;
  total: number;
}

function validate(line: Line): void {
  if (line.sku === "") throw new PricingError("sku must not be empty");
  if (!Number.isInteger(line.unitCents) || line.unitCents < 0) {
    throw new PricingError("unit price must be a non-negative integer");
  }
  if (!Number.isInteger(line.quantity) || line.quantity < 1) {
    throw new PricingError("quantity must be a positive integer");
  }
}

export function quote(lines: Line[], discountPercent = 0): Quote {
  if (lines.length === 0) throw new PricingError("an order needs at least one line");
  if (discountPercent < 0 || discountPercent > MAX_DISCOUNT_PERCENT) {
    throw new PricingError(`discount must be between 0 and ${MAX_DISCOUNT_PERCENT} percent`);
  }
  lines.forEach(validate);
  const subtotal = lines.reduce((sum, l) => sum + l.unitCents * l.quantity, 0);
  const discount = Math.floor((subtotal * discountPercent) / 100);
  const shipping = subtotal - discount >= FREE_SHIPPING_CENTS ? 0 : FLAT_SHIPPING_CENTS;
  return { subtotal, discount, shipping, total: subtotal - discount + shipping };
}
