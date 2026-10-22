import pytest

from pricing.core import FLAT_SHIPPING_CENTS, Line, PricingError, quote


def line(cents=1_000, qty=1, sku="A"):
    return Line(sku, cents, qty)


@pytest.mark.parametrize(
    ("sku", "cents", "qty", "message"),
    [
        ("", 1, 1, r"^sku must not be empty$"),
        ("A", -1, 1, r"^unit price must not be negative$"),
        ("A", 1, 0, r"^quantity must be at least 1$"),
    ],
)
def test_line_validation(sku, cents, qty, message):
    with pytest.raises(PricingError, match=message):
        Line(sku, cents, qty)


def test_zero_price_and_single_unit_are_valid():
    assert quote([line(0, 1)]).subtotal == 0


def test_small_order_pays_shipping():
    q = quote([line(1_000, 2)])
    assert (q.subtotal, q.discount, q.shipping, q.total) == (2_000, 0, FLAT_SHIPPING_CENTS, 2_800)


def test_free_shipping_at_threshold():
    assert quote([line(10_000)]).shipping == 0
    assert quote([line(9_999)]).shipping == FLAT_SHIPPING_CENTS


def test_threshold_applies_after_discount():
    q = quote([line(11_000)], discount_percent=10)
    assert (q.discount, q.shipping, q.total) == (1_100, FLAT_SHIPPING_CENTS, 10_700)


def test_discount_rounds_down():
    assert quote([line(15)], discount_percent=10).discount == 1


@pytest.mark.parametrize("percent", [0, 50])
def test_discount_bounds_inclusive(percent):
    quote([line()], percent)


@pytest.mark.parametrize("percent", [-1, 51])
def test_discount_out_of_range(percent):
    with pytest.raises(PricingError, match=r"^discount must be between 0 and 50 percent$"):
        quote([line()], percent)


def test_empty_order():
    with pytest.raises(PricingError, match=r"^an order needs at least one line$"):
        quote([])
