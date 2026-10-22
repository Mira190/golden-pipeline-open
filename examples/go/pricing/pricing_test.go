package pricing

import (
	"errors"
	"testing"
	"testing/quick"
)

func line(cents, qty int) Line { return Line{SKU: "A", UnitCents: cents, Quantity: qty} }

func TestPrice(t *testing.T) {
	tests := []struct {
		name     string
		lines    []Line
		discount int
		want     Quote
	}{
		{"flat shipping", []Line{line(1_000, 2)}, 0, Quote{2_000, 0, FlatShippingCents, 2_800}},
		{"free at threshold", []Line{line(10_000, 1)}, 0, Quote{10_000, 0, 0, 10_000}},
		{"just below threshold", []Line{line(9_999, 1)}, 0, Quote{9_999, 0, FlatShippingCents, 10_799}},
		{"threshold after discount", []Line{line(11_000, 1)}, 10, Quote{11_000, 1_100, FlatShippingCents, 10_700}},
		{"discount rounds down", []Line{line(15, 1)}, 10, Quote{15, 1, FlatShippingCents, 814}},
		{"max discount", []Line{line(100, 1)}, 50, Quote{100, 50, FlatShippingCents, 850}},
		{"several lines", []Line{line(100, 3), line(50, 2)}, 0, Quote{400, 0, FlatShippingCents, 1_200}},
		{"zero price", []Line{line(0, 1)}, 0, Quote{0, 0, FlatShippingCents, 800}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Price(tc.lines, tc.discount)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Fatalf("Price() = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestPriceRejectsInvalidOrders(t *testing.T) {
	tests := []struct {
		name     string
		lines    []Line
		discount int
		message  string
	}{
		{"empty", nil, 0, "invalid order: an order needs at least one line"},
		{"negative discount", []Line{line(1, 1)}, -1, "invalid order: discount must be between 0 and 50 percent"},
		{"discount too high", []Line{line(1, 1)}, 51, "invalid order: discount must be between 0 and 50 percent"},
		{"empty sku", []Line{{UnitCents: 1, Quantity: 1}}, 0, "invalid order: sku must not be empty"},
		{"negative price", []Line{line(-1, 1)}, 0, "invalid order: unit price must not be negative"},
		{"zero quantity", []Line{line(1, 0)}, 0, "invalid order: quantity must be at least 1"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Price(tc.lines, tc.discount)
			if !errors.Is(err, ErrInvalid) || err.Error() != tc.message {
				t.Fatalf("Price() error = %v, want %q", err, tc.message)
			}
		})
	}
}

// Property: the total always equals subtotal - discount + shipping.
func TestPriceTotalIsConsistent(t *testing.T) {
	property := func(cents, qty uint16, discount uint8) bool {
		q, err := Price([]Line{line(int(cents), int(qty)+1)}, int(discount)%51)
		return err == nil && q.Total == q.Subtotal-q.Discount+q.Shipping &&
			q.Discount <= q.Subtotal/2 && (q.Shipping == 0) == (q.Subtotal-q.Discount >= FreeShippingCents)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}
