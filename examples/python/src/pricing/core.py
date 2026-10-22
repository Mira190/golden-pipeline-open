"""Order pricing: pure functions, money in integer cents."""

from __future__ import annotations

from dataclasses import dataclass

FREE_SHIPPING_CENTS = 10_000
FLAT_SHIPPING_CENTS = 800
MAX_DISCOUNT_PERCENT = 50


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class Line:
    sku: str
    unit_cents: int
    quantity: int

    def __post_init__(self) -> None:
        if not self.sku:
            raise PricingError("sku must not be empty")
        if self.unit_cents < 0:
            raise PricingError("unit price must not be negative")
        if self.quantity < 1:
            raise PricingError("quantity must be at least 1")


@dataclass(frozen=True)
class Quote:
    subtotal: int
    discount: int
    shipping: int

    @property
    def total(self) -> int:
        return self.subtotal - self.discount + self.shipping


def quote(lines: list[Line], discount_percent: int = 0) -> Quote:
    """Price an order. The discount rounds half-down so customers never get extra cents."""
    if not lines:
        raise PricingError("an order needs at least one line")
    if not 0 <= discount_percent <= MAX_DISCOUNT_PERCENT:
        raise PricingError(f"discount must be between 0 and {MAX_DISCOUNT_PERCENT} percent")
    subtotal = sum(line.unit_cents * line.quantity for line in lines)
    discount = subtotal * discount_percent // 100
    shipping = 0 if subtotal - discount >= FREE_SHIPPING_CENTS else FLAT_SHIPPING_CENTS
    return Quote(subtotal, discount, shipping)
