"""`python -m pricing 1000x2 250x1 --discount 10` prints a quote."""

from __future__ import annotations

import argparse

from pricing.core import Line, PricingError, quote


def parse_line(spec: str) -> Line:
    cents, _, qty = spec.partition("x")
    try:
        return Line("item", int(cents), int(qty or "1"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bad line {spec!r}: {exc}") from exc


def main(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(prog="pricing")
    parser.add_argument("lines", nargs="+", type=parse_line, metavar="CENTSxQTY")
    parser.add_argument("--discount", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        q = quote(args.lines, args.discount)
    except PricingError as exc:
        parser.error(str(exc))
    return f"subtotal={q.subtotal} discount={q.discount} shipping={q.shipping} total={q.total}"
