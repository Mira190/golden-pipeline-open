import pytest

from pricing.cli import main, parse_line


def test_prints_quote():
    assert main(["1000x2"]) == "subtotal=2000 discount=0 shipping=800 total=2800"


def test_discount_and_default_quantity():
    assert main(["12000", "--discount", "10"]) == (
        "subtotal=12000 discount=1200 shipping=0 total=10800"
    )


def test_parse_line():
    line = parse_line("250x3")
    assert (line.unit_cents, line.quantity) == (250, 3)


@pytest.mark.parametrize("argv", [["abc"], ["100x0"], ["100", "--discount", "90"]])
def test_invalid_input_exits_with_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
