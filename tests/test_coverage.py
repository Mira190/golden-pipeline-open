import json

import pytest

from golden import coverage

COBERTURA = """<?xml version="1.0" ?>
<coverage line-rate="0.75" lines-covered="3" lines-valid="4" version="7">
  <packages><package><classes><class filename="a.py"><lines>
    <line number="1" hits="1"/><line number="2" hits="1"/>
    <line number="3" hits="1"/><line number="4" hits="0"/>
  </lines></class></classes></package></packages>
</coverage>"""

COBERTURA_NO_TOTALS = """<coverage><packages><package><classes><class filename="a.go"><lines>
    <line number="1" hits="2"/><line number="2" hits="0"/>
  </lines></class></classes></package></packages></coverage>"""

LCOV = """TN:
SF:src/a.ts
DA:1,1
LF:10
LH:7
end_of_record
SF:src/b.ts
LF:10
LH:3
end_of_record
"""

GOCOVER = """mode: atomic
example.com/m/a.go:3.10,5.2 2 1

example.com/m/a.go:7.10,9.2 3 0
example.com/m/a.go:7.10,9.2 3 4
example.com/m/my dir/b.go:1.1,2.2 5 0
example.com/m/c.go:1.1,2.2 0 1
"""


@pytest.mark.parametrize(
    ("name", "content", "fmt", "percent"),
    [
        ("coverage.xml", COBERTURA, "cobertura", 75.0),
        ("coverage.xml", COBERTURA_NO_TOTALS, "cobertura", 50.0),
        ("lcov.info", LCOV, "lcov", 50.0),
        ("coverage.out", GOCOVER, "gocover", 50.0),
    ],
)
def test_read_totals_detects_format(tmp_path, name, content, fmt, percent):
    path = tmp_path / name
    path.write_text(content)
    assert coverage.detect_format(path) == fmt
    assert coverage.read_totals(path).percent == percent
    assert coverage.read_totals(path, fmt).percent == percent


def test_empty_report_is_fully_covered():
    assert coverage.Totals(0, 0).percent == 100.0


def test_baseline_roundtrip(tmp_path):
    path = tmp_path / "deep" / "baseline.json"
    assert coverage.read_baseline(path) is None
    coverage.write_baseline(path, 91.5, "abc")
    assert coverage.read_baseline(path) == 91.5
    assert json.loads(path.read_text())["sha"] == "abc"


@pytest.mark.parametrize(
    ("current", "baseline", "ok", "delta"),
    [
        (90.0, 90.0, True, 0.0),
        (89.5, 90.0, True, -0.5),
        (89.49, 90.0, False, -0.51),
        (95.0, 90.0, True, 5.0),
    ],
)
def test_ratchet(current, baseline, ok, delta):
    assert coverage.ratchet(current, baseline, 0.5) == (ok, delta)


@pytest.mark.parametrize("content", ["[]", '{"percent": 80}', '"80"'])
def test_baseline_without_line_percent_fails_closed(tmp_path, content):
    path = tmp_path / "baseline.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="has no line_percent"):
        coverage.read_baseline(path)


def test_truncated_cobertura_fails_closed(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text('<?xml version="1.0" ?><coverage lines-covered="3"')
    with pytest.raises(ValueError, match="unreadable Cobertura report"):
        coverage.read_totals(path)
