from hypothesis import given
from hypothesis import strategies as st

from pricing.core import FREE_SHIPPING_CENTS, Line, quote

lines = st.lists(
    st.builds(
        Line,
        sku=st.text(min_size=1, max_size=4),
        unit_cents=st.integers(0, 1_000_000),
        quantity=st.integers(1, 100),
    ),
    min_size=1,
    max_size=10,
)
percents = st.integers(0, 50)


@given(lines, percents)
def test_total_is_consistent(order, percent):
    q = quote(order, percent)
    assert q.total == q.subtotal - q.discount + q.shipping
    assert 0 <= q.discount <= q.subtotal // 2


@given(lines, percents)
def test_free_shipping_rule(order, percent):
    q = quote(order, percent)
    assert (q.shipping == 0) == (q.subtotal - q.discount >= FREE_SHIPPING_CENTS)


@given(lines)
def test_line_order_is_irrelevant(order):
    assert quote(order) == quote(order[::-1])
