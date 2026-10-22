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


@pytest.mark.parametrize(
    ("name", "content"),
    [("lcov.info", ""), ("coverage.xml", "<coverage/>"), ("coverage.out", "mode: set\n")],
)
def test_report_without_coverable_lines_fails_closed(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    with pytest.raises(ValueError, match="no coverable lines"):
        coverage.read_totals(path)


def test_cobertura_counts_a_single_hit_as_covered(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text('<coverage><line hits="1"/><line hits="0"/><line hits="0"/></coverage>')
    assert coverage.read_totals(path) == coverage.Totals(1, 3)


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
        (90.004, 90.0, True, 0.0),
    ],
)
def test_ratchet(current, baseline, ok, delta):
    assert coverage.ratchet(current, baseline, 0.5) == (ok, delta)


@pytest.mark.parametrize("content", ["[]", '{"percent": 80}', '"80"'])
def test_baseline_without_line_percent_fails_closed(tmp_path, content):
    path = tmp_path / "baseline.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="is malformed"):
        coverage.read_baseline(path)


def test_truncated_cobertura_fails_closed(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text('<?xml version="1.0" ?><coverage lines-covered="3"')
    with pytest.raises(ValueError, match="unreadable Cobertura report"):
        coverage.read_totals(path)


def test_measured_files_cobertura_joins_sources(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(
        "<coverage><sources><source>/w/src</source><source></source></sources>"
        '<packages><package><classes><class filename="pkg/a.py"/><class name="no-file"/>'
        "</classes></package></packages></coverage>"
    )
    assert coverage.measured_files(path) == {"pkg/a.py", "/w/src/pkg/a.py"}


def test_measured_files_lcov_and_gocover(tmp_path):
    lcov = tmp_path / "lcov.info"
    lcov.write_text(LCOV)
    assert coverage.measured_files(lcov) == {"src/a.ts", "src/b.ts"}
    go = tmp_path / "coverage.out"
    go.write_text(GOCOVER)
    assert coverage.measured_files(go) == {
        "example.com/m/a.go",
        "example.com/m/my dir/b.go",
        "example.com/m/c.go",
    }


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("src/pkg/a.py", True),
        ("pkg/a.py", True),
        ("a.py", True),
        ("src/pkg/b.py", False),
        ("other/pkg/a.py", False),
        ("xa.py", False),
    ],
)
def test_is_measured(file, expected):
    assert coverage.is_measured(file, {"/w/src/pkg/a.py"}) is expected


def test_short_report_names_do_not_cover_deeper_files():
    assert not coverage.is_measured("pkg/__init__.py", {"__init__.py"})


# ------------------------------------------------------------- found by tests/test_fuzz.py
# A report the reader cannot use is a ValueError that names the report: not a UnicodeDecodeError,
# an ElementTree.ParseError (which is no ValueError at all), a KeyError or an OverflowError.
@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("coverage.xml", b"\x80", "is not UTF-8 text"),
        ("lcov.info", b"\xff\xfe", "is not UTF-8 text"),
        ("coverage.out", b"\xef\xbb\xbfmode: set\n\xff", "is not UTF-8 text"),
        ("coverage.xml", b"", "unreadable Cobertura report"),
        ("coverage.xml", b"{\n", "unreadable Cobertura report"),
        ("coverage.xml", b'<coverage lines-valid="3"/>', "attribute lines-covered is missing"),
        ("coverage.xml", b'<coverage lines-valid="x" lines-covered="1"/>', "lines-valid='x'"),
        ("coverage.xml", b'<coverage><line hits="1.5"/></coverage>', "hits='1.5' is not a whole"),
        ("coverage.out", b"mode: set\na.go:1.1,2.2 1\n", "malformed gocover coverage report"),
        ("coverage.out", b"mode: set\na.go:1.1,2.2 x 1\n", "malformed gocover coverage report"),
    ],
)
def test_a_report_the_reader_cannot_use_is_a_value_error_that_names_it(
    tmp_path, name, content, reason
):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(ValueError, match=reason) as raised:
        coverage.read_totals(path)
    assert type(raised.value) is ValueError
    assert str(path) in str(raised.value)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("coverage.xml", b""),
        ("coverage.xml", b"{\n"),
        ("coverage.xml", b"<coverage><sources><source>/w</source></sources>"),
        ("coverage.xml", b"\xff\xfe"),
        ("lcov.info", b"\xff\xfe"),
        ("coverage.out", b"\xff\xfe"),
    ],
)
def test_the_files_of_an_unreadable_report_are_a_value_error(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(
        ValueError, match=r"unreadable Cobertura report|is not UTF-8 text"
    ) as raised:
        coverage.measured_files(path)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(
    ("name", "content", "covered", "total"),
    [
        ("lcov.info", b"SF:a\nLF:1\nLH:5\nend_of_record\n", 5, 1),
        ("coverage.xml", b'<coverage lines-valid="3" lines-covered="4"/>', 4, 3),
        ("coverage.xml", b'<coverage lines-valid="-5" lines-covered="-3"/>', -3, -5),
        ("coverage.xml", b'<coverage lines-valid="5" lines-covered="-1"/>', -1, 5),
    ],
)
def test_a_report_that_covers_more_lines_than_it_has_is_not_a_report(
    tmp_path, name, content, covered, total
):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(ValueError, match=f"is inconsistent: {covered} of {total} lines covered"):
        coverage.read_totals(path)


def test_a_report_that_covers_every_line_or_none_is_consistent(tmp_path):
    path = tmp_path / "lcov.info"
    path.write_text("SF:a\nLF:4\nLH:4\nend_of_record\nSF:b\nLF:2\nLH:0\nend_of_record\n")
    assert coverage.read_totals(path) == coverage.Totals(4, 6)


def test_a_negative_statement_count_cannot_make_a_gocover_profile_consistent(tmp_path):
    # 5 - 3 = 2 statements, all covered: looks like 100%, but a block cannot have -3 statements.
    path = tmp_path / "coverage.out"
    path.write_text("mode: set\nm/a.go:1.1,2.2 5 1\nm/b.go:1.1,2.2 -3 1\n")
    with pytest.raises(ValueError, match=r"the block m/b.go:1.1,2.2 has -3 statements"):
        coverage.read_totals(path)


@pytest.mark.parametrize(
    "content",
    [
        '{"line_percent": 100.01}',
        '{"line_percent": 101}',
        '{"line_percent": -0.01}',
        '{"line_percent": -1}',
        '{"line_percent": -Infinity}',  # every change would pass the ratchet
        '{"line_percent": Infinity}',
        '{"line_percent": NaN}',
        '{"line_percent": true}',
        '{"line_percent": "87.5"}',
        '{"line_percent": null}',
        '{"line_percent": ' + "1" + "0" * 400 + "}",  # float() of it is an OverflowError
        '{"line_percent": [87.5]}',
        "",
        '"',
        '""\x00',
        "﻿" + '{"line_percent": 80}',
        pytest.param("[" * 100_000 + "]" * 100_000, id="deeply-nested"),
    ],
)
def test_a_baseline_that_is_no_percentage_is_an_error(tmp_path, content):
    path = tmp_path / "baseline.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=r"baseline .*baseline\.json is malformed") as raised:
        coverage.read_baseline(path)
    assert type(raised.value) is ValueError


def test_a_baseline_that_is_not_utf8_is_an_error(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match=r"baseline .* is malformed"):
        coverage.read_baseline(path)


@pytest.mark.parametrize(
    ("content", "percent"),
    [
        ('{"line_percent": 0}', 0.0),
        ('{"line_percent": 87}', 87.0),
        ('{"line_percent": 100}', 100.0),
    ],
)
def test_the_whole_range_of_percentages_is_a_baseline(tmp_path, content, percent):
    path = tmp_path / "baseline.json"
    path.write_text(content)
    baseline = coverage.read_baseline(path)
    assert baseline == percent
    assert type(baseline) is float


def test_a_line_without_hits_is_not_covered(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text('<coverage><line number="1"/><line number="2" hits="2"/></coverage>')
    assert coverage.read_totals(path) == coverage.Totals(1, 2)


def test_an_explicit_format_overrides_the_file_name(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text("SF:a\nLF:4\nLH:3\nend_of_record\n")
    assert coverage.read_totals(path, "lcov") == coverage.Totals(3, 4)


def test_gocover_blocks_are_separated_by_single_spaces(tmp_path):
    path = tmp_path / "coverage.out"
    path.write_text("mode: set\nm/a.go:1.1,2.2\t1\t1\n")
    with pytest.raises(ValueError, match="malformed gocover coverage report"):
        coverage.read_totals(path)


def test_gocover_names_may_hold_colons(tmp_path):
    path = tmp_path / "coverage.out"
    path.write_text("mode: set\nc:/w/a.go:1.1,2.2 1 1\n")
    assert coverage.measured_files(path) == {"c:/w/a.go"}


def test_measured_files_reads_the_sources_and_the_classes_only(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text(
        "<coverage><sources><source>/w/boxX/</source></sources><packages>"
        '<package filename="p.py">text<classes><class filename="a.py">more</class></classes>'
        "</package></packages></coverage>"
    )
    assert coverage.measured_files(path) == {"a.py", "/w/boxX/a.py"}
