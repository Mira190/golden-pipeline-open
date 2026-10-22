// Package pricing prices orders. Money is in integer cents.
package pricing

import (
	"errors"
	"fmt"
)

// Pricing rules, in cents and percent.
const (
	FreeShippingCents  = 10_000
	FlatShippingCents  = 800
	MaxDiscountPercent = 50
)

// ErrInvalid wraps every validation failure.
var ErrInvalid = errors.New("invalid order")

// Line is one order line.
type Line struct {
	SKU       string
	UnitCents int
	Quantity  int
}

// Quote is the priced order.
type Quote struct {
	Subtotal, Discount, Shipping, Total int
}

func (l Line) validate() error {
	switch {
	case l.SKU == "":
		return fmt.Errorf("%w: sku must not be empty", ErrInvalid)
	case l.UnitCents < 0:
		return fmt.Errorf("%w: unit price must not be negative", ErrInvalid)
	case l.Quantity < 1:
		return fmt.Errorf("%w: quantity must be at least 1", ErrInvalid)
	}
	return nil
}

// Price quotes an order; the discount rounds down so customers never get extra cents.
func Price(lines []Line, discountPercent int) (Quote, error) {
	if len(lines) == 0 {
		return Quote{}, fmt.Errorf("%w: an order needs at least one line", ErrInvalid)
	}
	if discountPercent < 0 || discountPercent > MaxDiscountPercent {
		return Quote{}, fmt.Errorf("%w: discount must be between 0 and %d percent", ErrInvalid, MaxDiscountPercent)
	}
	subtotal := 0
	for _, l := range lines {
		if err := l.validate(); err != nil {
			return Quote{}, err
		}
		subtotal += l.UnitCents * l.Quantity
	}
	discount := subtotal * discountPercent / 100
	shipping := FlatShippingCents
	if subtotal-discount >= FreeShippingCents {
		shipping = 0
	}
	return Quote{subtotal, discount, shipping, subtotal - discount + shipping}, nil
}
