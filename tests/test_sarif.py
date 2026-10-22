"""Static-analysis gate: SARIF parsing, narrowing to changed lines, `golden sast`."""

import datetime as dt
import json
from pathlib import Path

import pytest

from golden import cli, sarif
from golden.sarif import Finding


def location(uri="a.py", start=3, end=None, base=None):
    artifact = {"uri": uri}
    if base:
        artifact["uriBaseId"] = base
    physical = {"artifactLocation": artifact}
    if start is not None:
        physical["region"] = {"startLine": start}
        if end is not None:
            physical["region"]["endLine"] = end
    return {"physicalLocation": physical}


def result(rid="R1", level="error", text=None, **fields):
    found = {
        "ruleId": rid,
        "message": {"text": f"{rid} found" if text is None else text},
        "locations": [location()],
    }
    if level:
        found["level"] = level
    return {**found, **fields}


def run(*results, rules=(), **fields):
    tool = {"driver": {"name": "tool", "rules": list(rules)}, **fields.pop("tool", {})}
    return {"tool": tool, "results": list(results), **fields}


def log(*runs):
    return {"version": "2.1.0", "runs": list(runs)}


def write(tmp_path, data, name="report.sarif"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def read(tmp_path, *runs):
    return sarif.read([write(tmp_path, log(*runs))])


def one(tmp_path, found, **fields):
    (finding,) = read(tmp_path, run(found, **fields))
    return finding


# ----------------------------------------------------------------- levels and rules
def test_a_result_carries_its_own_level(tmp_path):
    rules = [{"id": "R1", "defaultConfiguration": {"level": "error"}}]
    assert one(tmp_path, result(level="note", ruleIndex=0), rules=rules).level == "note"


@pytest.mark.parametrize(
    "found",
    [
        result(level=None, ruleIndex=0),
        result(level=None, ruleIndex=7),  # out of range: the id still finds the rule
        result(level=None, ruleIndex=-1),
        result(level=None),
        result(level=None, ruleId=None, rule={"id": "R1"}),
    ],
)
def test_the_rule_default_applies_when_the_result_has_no_level(tmp_path, found):
    rules = [
        {"id": "R1", "defaultConfiguration": {"level": "error"}},
        {"id": "R0", "defaultConfiguration": {"level": "note"}},
    ]
    assert one(tmp_path, found, rules=rules).level == "error"


def test_the_rule_is_found_by_id_when_there_is_no_index(tmp_path):
    rules = [{"id": "R0", "defaultConfiguration": {"level": "note"}}, {"id": "R1"}]
    rules[1]["defaultConfiguration"] = {"level": "error"}
    assert one(tmp_path, result(level=None, ruleId="R1"), rules=rules).level == "error"
    assert one(tmp_path, result(level=None, ruleId="R0"), rules=rules).level == "note"


def test_the_level_defaults_to_warning(tmp_path):
    assert one(tmp_path, result(level=None)).level == "warning"
    rules = [{"id": "R1", "defaultConfiguration": {}}]
    assert one(tmp_path, result(level=None), rules=rules).level == "warning"
    rules = [{"id": "R1", "defaultConfiguration": {"level": None}}]
    assert one(tmp_path, result(level=None), rules=rules).level == "warning"
    assert one(tmp_path, result(level=None, ruleId="other"), rules=rules).level == "warning"


def test_codeql_keeps_rule_severities_in_extensions(tmp_path):
    extensions = [
        {
            "name": "other/queries",
            "rules": [{"id": "x/y", "defaultConfiguration": {"level": "note"}}],
        },
        {
            "name": "codeql/python-queries",
            "rules": [
                {"id": "py/a", "defaultConfiguration": {"level": "note"}},
                {"id": "py/sql-injection", "defaultConfiguration": {"level": "error"}},
            ],
        },
    ]
    by_index = result(
        level=None,
        ruleId="py/sql-injection",
        ruleIndex=1,
        rule={"id": "py/sql-injection", "index": 1, "toolComponent": {"name": "x", "index": 1}},
    )
    assert one(tmp_path, by_index, tool={"extensions": extensions}).level == "error"
    by_name = result(
        level=None,
        ruleId="py/sql-injection",
        rule={"id": "py/sql-injection", "toolComponent": {"name": "codeql/python-queries"}},
    )
    assert one(tmp_path, by_name, tool={"extensions": extensions}).level == "error"
    unnamed = result(level=None, ruleId="py/sql-injection")
    assert one(tmp_path, unnamed, tool={"extensions": extensions}).level == "warning"


def test_a_component_index_outside_the_extensions_falls_back_to_the_driver(tmp_path):
    rules = [{"id": "R1", "defaultConfiguration": {"level": "note"}}]
    found = result(level=None, rule={"id": "R1", "toolComponent": {"index": 3}})
    assert one(tmp_path, found, rules=rules, tool={"extensions": []}).level == "note"
    found = result(level=None, rule={"id": "R1", "toolComponent": {"name": "gone", "index": -1}})
    assert one(tmp_path, found, rules=rules, tool={"extensions": []}).level == "note"


def test_a_rule_index_selects_the_rule_where_the_id_names_none(tmp_path):
    rules = [
        {"id": "R0", "defaultConfiguration": {"level": "note"}},
        {"id": "R1", "defaultConfiguration": {"level": "error"}},
    ]

    def level(**fields):
        return one(tmp_path, result(level=None, ruleId="gone", **fields), rules=rules).level

    assert level(ruleIndex=0) == "note"
    assert level(ruleIndex=1) == "error"
    assert level(rule={"index": 1}) == "error"
    assert level(rule={"index": 0}) == "note"
    assert level(ruleIndex=0, rule={"index": 1}) == "note"  # the result's own index wins
    assert level(ruleIndex=2) == "warning"  # one past the end: no such rule
    assert level(rule={"index": 2}) == "warning"


def test_an_index_one_past_the_rules_falls_back_to_the_id(tmp_path):
    rules = [{"id": "R0"}, {"id": "R1", "defaultConfiguration": {"level": "error"}}]
    assert one(tmp_path, result(level=None, ruleId="R1", ruleIndex=2), rules=rules).level == "error"
    found = result(level=None, ruleId="R1", rule={"index": 2})
    assert one(tmp_path, found, rules=rules).level == "error"


def test_an_extension_is_picked_by_index_first_or_last_and_never_past_the_end(tmp_path):
    def rules(level):
        return [{"id": "R1", "defaultConfiguration": {"level": level}}]

    tool = {
        "extensions": [
            {"name": "first", "rules": rules("note")},
            {"name": "last", "rules": rules("error")},
        ]
    }

    def level(index):
        found = result(level=None, rule={"id": "R1", "toolComponent": {"index": index}})
        return one(tmp_path, found, rules=rules("warning"), tool=tool).level

    assert [level(0), level(1), level(2)] == ["note", "error", "warning"]


def test_results_that_are_not_failures_are_not_findings(tmp_path):
    kinds = ("pass", "notApplicable", "informational", "open", "review")
    found = [result(level=None, kind=kind, text=kind) for kind in kinds]
    assert read(tmp_path, run(*found)) == []
    explicit = result(level="error", kind="review")
    assert one(tmp_path, explicit).level == "error"
    assert one(tmp_path, result(level=None, kind="fail")).level == "warning"


def test_results_without_severity_are_not_findings(tmp_path):
    assert read(tmp_path, run(result(level="none"))) == []
    rules = [{"id": "R1", "defaultConfiguration": {"level": "none"}}]
    assert read(tmp_path, run(result(level=None), rules=rules)) == []


def test_an_unknown_level_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="unknown level 'critical'"):
        read(tmp_path, run(result(level="critical")))
    with pytest.raises(ValueError, match="malformed SARIF report"):
        read(tmp_path, run(result(level=["error"])))


# ----------------------------------------------------------------- what is a finding
def test_a_finding_has_rule_message_and_location(tmp_path):
    found = result("py/x", "warning", "a  message\n over lines", locations=[location("a.py", 3, 5)])
    assert one(tmp_path, found) == Finding(
        "py/x", "warning", "a message over lines", "a.py", (3, 5)
    )


def test_the_rule_id_can_come_from_the_rule_reference(tmp_path):
    found = result(ruleId=None, rule={"id": "ref/id"})
    assert one(tmp_path, found).rule == "ref/id"
    assert one(tmp_path, result(ruleId=None)).rule == "unknown-rule"


def test_messages_fall_back_to_markdown_then_to_a_placeholder(tmp_path):
    assert one(tmp_path, result(message={"markdown": " **bold**\n"})).message == "**bold**"
    assert one(tmp_path, result(message={"text": "", "markdown": "md"})).message == "md"
    assert one(tmp_path, result(message={})).message == "(no message)"
    assert one(tmp_path, result(message={"text": " \n "})).message == "(no message)"
    assert one(tmp_path, {"ruleId": "R", "level": "error", "locations": [location()]}).message == (
        "(no message)"
    )


def test_suppressed_results_are_skipped(tmp_path):
    accepted = result(text="accepted", suppressions=[{"kind": "inSource", "status": "accepted"}])
    no_status = result(text="no status", suppressions=[{"kind": "inSource"}])
    external = result(text="external", suppressions=[{"kind": "external", "status": "accepted"}])
    assert read(tmp_path, run(accepted, no_status, external)) == []


def test_a_suppression_nobody_accepted_does_not_hide_the_finding(tmp_path):
    under_review = result(text="review", suppressions=[{"status": "underReview"}])
    rejected = result(text="rejected", suppressions=[{"status": "rejected"}])
    none = result(text="none", suppressions=[])
    mixed = result(text="mixed", suppressions=[{"status": "rejected"}, {"status": "accepted"}])
    found = read(tmp_path, run(under_review, rejected, none, mixed))
    assert [f.message for f in found] == ["review", "rejected", "none"]


def test_findings_a_baseline_already_knows_are_skipped(tmp_path):
    states = ("unchanged", "absent", "new", "updated")
    found = read(tmp_path, run(*(result(text=s, baselineState=s) for s in states)))
    assert [f.message for f in found] == ["new", "updated"]
    assert [f.message for f in read(tmp_path, run(result(text="plain")))] == ["plain"]


def test_skipped_results_need_no_location(tmp_path):
    bare = {"ruleId": "R1", "level": "error", "message": {"text": "x"}}
    skipped = [
        {**bare, "suppressions": [{"status": "accepted"}]},
        {**bare, "baselineState": "unchanged"},
        {**bare, "level": "none"},
    ]
    assert read(tmp_path, run(*skipped)) == []


def test_duplicates_across_runs_and_reports_count_once(tmp_path):
    first = write(tmp_path, log(run(result("A"), result("B")), run(result("A"))), "one.sarif")
    second = write(tmp_path, log(run(result("B"), result("C"))), "two.sarif")
    assert [f.rule for f in sarif.read([first, second])] == ["A", "B", "C"]


def test_every_run_of_every_report_is_read(tmp_path):
    first = write(tmp_path, log(run(result("A")), run(result("B"))), "one.sarif")
    second = write(tmp_path, log(run(result("C"))), "two.sarif")
    assert [f.rule for f in sarif.read([first, second])] == ["A", "B", "C"]


# ----------------------------------------------------------------- where
def test_a_region_is_one_line_or_a_range(tmp_path):
    assert one(tmp_path, result(locations=[location(start=7)])).region == (7, 7)
    assert one(tmp_path, result(locations=[location(start=7, end=9)])).region == (7, 9)
    assert one(tmp_path, result(locations=[location(start=7, end=7)])).region == (7, 7)


def test_a_finding_about_the_whole_file_has_no_region(tmp_path):
    assert one(tmp_path, result(locations=[location(start=None)])).region is None
    offsets = {
        "physicalLocation": {"artifactLocation": {"uri": "a.py"}, "region": {"charOffset": 4}}
    }
    assert one(tmp_path, result(locations=[offsets])).region is None
    assert Finding("R", "error", "m", "a.py").line is None
    assert Finding("R", "error", "m", "a.py", (4, 6)).line == 4


@pytest.mark.parametrize(
    ("start", "end"), [(0, 1), (-2, 3), (5, 4), ("3", 3), (3, "4"), (True, 2), (3, 2.5), (3, None)]
)
def test_an_invalid_region_fails_closed(tmp_path, start, end):
    region = {"startLine": start, "endLine": end}
    found = result(
        locations=[{"physicalLocation": {"artifactLocation": {"uri": "a"}, "region": region}}]
    )
    if end is None:
        assert one(tmp_path, found).region == (3, 3)
        return
    with pytest.raises(ValueError, match="invalid region"):
        read(tmp_path, run(found))


def test_the_first_physical_location_is_the_finding(tmp_path):
    logical = {"logicalLocations": [{"name": "module.func"}]}
    found = result(locations=[logical, location("b.py", 2), location("c.py", 9)])
    assert (one(tmp_path, found).file, one(tmp_path, found).region) == ("b.py", (2, 2))


def test_uris_are_relative_to_the_working_directory(repo):
    cwd = Path.cwd().as_posix()
    uris = {
        "src/a.py": "src/a.py",
        "./src/a.py": "src/a.py",
        f"{cwd}/src/a.py": "src/a.py",
        f"file://{cwd}/src/a.py": "src/a.py",
        f"file://{cwd}/./src/a.py": "src/a.py",
        "file:///elsewhere/src/a.py": "/elsewhere/src/a.py",
        "/elsewhere/src/a.py": "/elsewhere/src/a.py",
        "src/my%20file%25.py": "src/my file%.py",
        "src/c#.py": "src/c#.py",
        "src/what?.py": "src/what?.py",
        "file:///elsewhere/my%20file.py": "/elsewhere/my file.py",
    }
    for uri, expected in uris.items():
        found = result(locations=[location(uri)])
        assert one(repo.root, found).file == expected, uri


def test_a_base_is_looked_up_in_the_originals(repo):
    cwd = Path.cwd().as_posix()
    bases = {
        "%ROOT%": {"uri": f"file://{cwd}/"},
        "%SUB%": {"uri": "services/api/"},
        "%DEEP%": {"uri": "api/", "uriBaseId": "%OUTER%"},
        "%OUTER%": {"uri": f"file://{cwd}/services"},
        "%LOOP%": {"uri": "x/", "uriBaseId": "%LOOP%"},
        "%EMPTY%": {},
    }
    expected = {
        "%ROOT%": "src/a.py",
        "%SUB%": "services/api/src/a.py",
        "%DEEP%": "services/api/src/a.py",
        "%LOOP%": "x/x/x/x/x/x/x/x/src/a.py",
        "%EMPTY%": "src/a.py",
        "%SRCROOT%": "src/a.py",  # not defined: the repository root
    }
    for base, path in expected.items():
        found = result(locations=[location("src/a.py", base=base)])
        assert one(repo.root, found, originalUriBaseIds=bases).file == path, base
    assert one(repo.root, result(locations=[location("src/a.py", base="%SRCROOT%")])).file == (
        "src/a.py"
    )


def test_an_absolute_uri_ignores_its_base(repo):
    bases = {"%SUB%": {"uri": "services/"}}
    found = result(locations=[location("/elsewhere/a.py", base="%SUB%")])
    assert one(repo.root, found, originalUriBaseIds=bases).file == "/elsewhere/a.py"


def test_a_result_without_a_location_fails_closed(tmp_path):
    bare = {"ruleId": "R1", "level": "error", "message": {"text": "x"}}
    for broken in (
        bare,
        {**bare, "locations": []},
        {**bare, "locations": None},
        {**bare, "locations": [{"logicalLocations": [{"name": "f"}]}]},
        {**bare, "locations": [{"physicalLocation": None}]},
        {**bare, "locations": [{"physicalLocation": {"region": {"startLine": 1}}}]},
        {**bare, "locations": [{"physicalLocation": {"artifactLocation": {}}}]},
        {**bare, "locations": [{"physicalLocation": {"artifactLocation": {"uri": ""}}}]},
        {**bare, "locations": [{"physicalLocation": {"artifactLocation": {"uri": 7}}}]},
    ):
        with pytest.raises(ValueError, match=r"report\.sarif"):
            read(tmp_path, run(broken))


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        (
            run(result(locations=[{"physicalLocation": {"artifactLocation": {}}}])),
            "a result's location has no artifact uri",
        ),
        ({"results": {"ruleId": "R"}}, "a run's `results` is not a list"),
        (run({"ruleId": "R", "level": "error"}), "a result has no physical location"),
    ],
)
def test_the_reason_for_a_malformed_result_is_named(tmp_path, broken, reason):
    with pytest.raises(ValueError, match=r"malformed SARIF report .*report\.sarif") as exc:
        read(tmp_path, broken)
    assert str(exc.value.__cause__) == reason


# ----------------------------------------------------------------- fail closed
def test_a_missing_report_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"missing\.sarif not found; did the analysis"):
        sarif.read([tmp_path / "missing.sarif"])


@pytest.mark.parametrize("content", ["", "{", "not json", "\x00\x01"])
def test_a_report_that_is_not_json_is_an_error(tmp_path, content):
    with pytest.raises(ValueError, match=r"report\.sarif is not valid JSON"):
        sarif.read([write(tmp_path, content)])


def test_a_binary_report_is_an_error(tmp_path):
    path = tmp_path / "report.sarif"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match="not valid JSON"):
        sarif.read([path])


@pytest.mark.parametrize(
    "content",
    [[], "null", 3, {}, {"version": "2.1.0"}, {"runs": None}, {"runs": {}}, {"runs": "x"}],
)
def test_a_report_without_runs_is_not_sarif(tmp_path, content):
    with pytest.raises(
        ValueError, match=r"report\.sarif is not a SARIF log: it has no `runs` list"
    ):
        sarif.read([write(tmp_path, content)])


@pytest.mark.parametrize(
    "broken",
    [
        "run",
        ["run"],
        {"results": {"ruleId": "R"}},
        {"results": "R"},
        {"results": ["R"]},
        {"results": [3]},
        {"tool": "driver", "results": [result(level=None)]},
        {"results": [{"ruleId": "R", "locations": [location()], "level": "error", "message": "m"}]},
        {"results": [{**result(), "rule": "R1"}]},
        {"results": [{**result(level=None), "ruleIndex": "0"}], "tool": {"driver": {"rules": 3}}},
    ],
)
def test_a_malformed_run_is_an_error(tmp_path, broken):
    with pytest.raises(ValueError, match=r"malformed SARIF report .*report\.sarif"):
        sarif.read([write(tmp_path, log(broken))])


def test_a_report_without_findings_is_fine(tmp_path):
    assert sarif.read([write(tmp_path, log())]) == []
    assert sarif.read([write(tmp_path, log(run()))]) == []
    assert sarif.read([write(tmp_path, log({"tool": {"driver": {}}, "results": None}))]) == []
    assert sarif.read([write(tmp_path, log({"results": None}))]) == []


def test_every_report_must_be_valid(tmp_path):
    good = write(tmp_path, log(run(result())), "good.sarif")
    with pytest.raises(FileNotFoundError):
        sarif.read([good, tmp_path / "missing.sarif"])


# ----------------------------------------------------------------- narrowing
def finding(lines, name="a.py", level="error", rule="R"):
    return Finding(rule, level, "m", name, lines)


@pytest.mark.parametrize(
    ("region", "inside"),
    [
        ((4, 4), False),
        ((3, 4), False),
        ((5, 5), True),
        ((7, 7), True),
        ((7, 9), True),
        ((8, 8), False),
        ((1, 20), True),
        ((6, 6), True),
        ((4, 5), True),
        ((1, 5), True),
        ((2, 3), False),
        ((10, 12), True),
        ((8, 9), False),
        ((9, 9), False),
        ((10, 10), True),
        ((11, 12), True),
        ((12, 12), False),
        ((13, 14), False),
    ],
)
def test_a_finding_is_in_scope_when_its_lines_overlap_a_changed_range(region, inside):
    lines = {"a.py": [(5, 7), (10, 11)]}
    assert finding(region).overlaps(lines) is inside


def test_a_finding_in_another_file_is_outside():
    assert not finding((5, 5), "b.py").overlaps({"a.py": [(1, 9)]})
    assert not finding((5, 5)).overlaps({})
    assert not finding((5, 5)).overlaps({"a.py": []})


def test_a_finding_about_the_whole_file_is_in_scope_in_a_changed_file():
    assert finding(None).overlaps({"a.py": [(40, 41)]})
    assert not finding(None).overlaps({"a.py": []})
    assert not finding(None, "b.py").overlaps({"a.py": [(40, 41)]})


def test_narrow_counts_what_lies_outside_the_change():
    inside, also = finding((5, 5), rule="in"), finding((6, 6), "b.py", rule="also")
    outside = [finding((1, 1)), finding((9, 9), "c.py")]
    lines = {"a.py": [(5, 5)], "b.py": [(6, 6)]}
    assert sarif.narrow([*outside, inside, also], lines) == ([inside, also], 2)
    assert sarif.narrow([], lines) == ([], 0)
    assert sarif.narrow(outside, lines) == ([], 2)


def test_narrow_puts_the_most_severe_first():
    lines = {"a.py": [(1, 99)], "b.py": [(1, 99)]}
    found = [
        finding((9, 9), "b.py", "note", "N"),
        finding((9, 9), "b.py", "error", "E2"),
        finding((9, 9), "a.py", "warning", "W"),
        finding((20, 20), "a.py", "error", "E1a"),
        finding((9, 9), "a.py", "error", "E1b"),
        finding((9, 9), "a.py", "error", "A"),
    ]
    changed, _ = sarif.narrow(found, lines)
    assert [f.rule for f in changed] == ["A", "E1b", "E1a", "E2", "W", "N"]


def test_narrow_orders_a_file_by_line_and_puts_a_whole_file_finding_first():
    lines = {"a.py": [(1, 99)]}
    found = [finding((20, 20), rule="A"), finding((2, 2), rule="B"), finding((1, 1), rule="C")]
    assert [f.rule for f in sarif.narrow(found, lines)[0]] == ["C", "B", "A"]
    found = [finding((1, 1), rule="A"), finding(None, rule="Z"), finding((2, 2), rule="B")]
    assert [f.rule for f in sarif.narrow(found, lines)[0]] == ["Z", "A", "B"]


def test_blocking_is_a_threshold_on_the_level():
    levels = ("note", "warning", "error")
    for minimum, expected in {
        "error": (False, False, True),
        "warning": (False, True, True),
        "note": (True, True, True),
    }.items():
        assert tuple(finding((1, 1), level=lv).blocks(minimum) for lv in levels) == expected


# ----------------------------------------------------------------- the verdict text
def test_detail_without_findings():
    assert sarif.detail([], 0, "error") == (
        "No findings on changed lines. Findings at level `error` or above block the merge."
    )


def test_detail_lists_findings_and_counts_the_pre_existing():
    changed = [
        Finding("py/sql", "error", "tainted query", "src/a.py", (3, 5)),
        Finding("py/pw", "warning", "weak hash", "src/b.py", (9, 9)),
        Finding("py/x", "warning", "other", "src/b.py", None),
    ]
    assert sarif.detail(changed, 1, "warning") == (
        "**3 findings** on changed lines (1 error, 2 warning). "
        "Findings at level `warning` or above block the merge.\n\n"
        "- `src/a.py:3-5` **error** `py/sql`: tainted query\n"
        "- `src/b.py:9` **warning** `py/pw`: weak hash\n"
        "- `src/b.py` **warning** `py/x`: other\n\n"
        "1 pre-existing finding outside this change (not blocking)."
    )


def test_detail_pluralises():
    one_note = [Finding("R", "note", "m", "a.py", (1, 1))]
    assert sarif.detail(one_note, 2, "note").startswith("**1 finding** on changed lines (1 note).")
    assert sarif.detail([], 2, "error").endswith(
        "\n\n2 pre-existing findings outside this change (not blocking)."
    )
    assert "pre-existing" not in sarif.detail(one_note, 0, "note")


def test_detail_is_always_one_line_per_finding():
    sneaky = Finding("R\n::error::x", "error", "m", "a\n::error::y.py", (1, 1))
    bullet = sarif.detail([sneaky], 0, "error").split("\n\n")[1]
    assert "\n" not in bullet
    assert bullet == "- `a ::error::y.py:1` **error** `R ::error::x`: m"


def test_detail_caps_the_list():
    found = [Finding("R", "error", f"m{n}", "a.py", (n, n)) for n in range(1, sarif.MAX_LISTED + 4)]
    text = sarif.detail(found, 0, "error")
    assert text.count("\n- `") == sarif.MAX_LISTED
    assert f"`a.py:{sarif.MAX_LISTED}`" in text
    assert f"`a.py:{sarif.MAX_LISTED + 1}`" not in text
    assert text.endswith("\n- ... and 3 more")
    exact = sarif.detail(found[: sarif.MAX_LISTED], 0, "error")
    assert "more" not in exact


# ----------------------------------------------------------------- golden sast
OLD = "".join(f"line {n}\n" for n in range(1, 11))


@pytest.fixture
def change(repo):
    """main has a.py (ten lines) and b.py; the feature branch rewrites a.py lines 4 and 5."""
    repo.commit({"a.py": OLD, "b.py": "x\n"}, "init")
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-qb", "feature")
    lines = OLD.splitlines()
    lines[3], lines[4] = "NEW 4", "NEW 5"
    repo.commit({"a.py": "\n".join(lines) + "\n"}, "feature")
    return base


def report(repo, *results, name="report.sarif", **fields):
    return write(repo.root, log(run(*results, **fields)), name)


def at(rule, level, uri, start, end=None, **fields):
    return result(rule, level, locations=[location(uri, start, end)], **fields)


def test_a_blocking_finding_on_a_changed_line_fails(change, repo, capsys):
    report(repo, at("py/sql", "error", "a.py", 4, text="tainted"))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert "::error file=a.py,line=4::py/sql: tainted\n" in out
    assert "#### ❌ Static analysis: failed" in out
    assert "**1 finding** on changed lines (1 error)." in out
    assert "- `a.py:4` **error** `py/sql`: tainted" in out


def test_findings_in_untouched_code_never_block(change, repo, capsys):
    report(
        repo,
        at("old/1", "error", "a.py", 2),
        at("old/2", "error", "a.py", 6, 9),
        at("old/3", "error", "b.py", 1),
        at("old/4", "warning", "gone.py", 1),
    )
    assert cli.main(["sast", "report.sarif", "--base", change]) == 0
    out = capsys.readouterr().out
    assert "#### ✅ Static analysis: passed" in out
    assert "No findings on changed lines." in out
    assert "4 pre-existing findings outside this change (not blocking)." in out
    assert "::error" not in out
    assert "::warning" not in out


def test_a_finding_spanning_into_the_change_is_in_scope(change, repo, capsys):
    report(repo, at("span/up", "error", "a.py", 1, 4), at("span/down", "error", "a.py", 5, 9))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert "::error file=a.py,line=1::span/up" in out
    assert "::error file=a.py,line=5::span/down" in out
    assert "pre-existing" not in out


def test_a_finding_just_outside_the_change_is_not(change, repo, capsys):
    report(repo, at("above", "error", "a.py", 1, 3), at("below", "error", "a.py", 6, 8))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 0
    assert "2 pre-existing findings outside" in capsys.readouterr().out


def test_non_blocking_findings_are_annotated_as_warnings(change, repo, capsys):
    report(repo, at("w/1", "warning", "a.py", 4), at("n/1", "note", "a.py", 5))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 0
    out = capsys.readouterr().out
    assert "::warning file=a.py,line=4::w/1: w/1 found\n" in out
    assert "::warning file=a.py,line=5::n/1: n/1 found\n" in out
    assert "::error" not in out
    assert "#### ✅ Static analysis: passed" in out
    assert "**2 findings** on changed lines (1 warning, 1 note)." in out
    assert "Findings at level `error` or above block the merge." in out


@pytest.mark.parametrize(
    ("minimum", "level", "code"),
    [
        ("error", "error", 1),
        ("error", "warning", 0),
        ("error", "note", 0),
        ("warning", "error", 1),
        ("warning", "warning", 1),
        ("warning", "note", 0),
        ("note", "error", 1),
        ("note", "warning", 1),
        ("note", "note", 1),
    ],
)
def test_min_level_sets_what_blocks(change, repo, capsys, minimum, level, code):
    report(repo, at("r/1", level, "a.py", 4))
    assert cli.main(["sast", "report.sarif", "--base", change, "--min-level", minimum]) == code
    out = capsys.readouterr().out
    assert f"Findings at level `{minimum}` or above block the merge." in out
    assert ("::error file=a.py,line=4" in out) is bool(code)
    assert ("::warning file=a.py,line=4" in out) is not bool(code)


def test_the_default_blocking_level_is_error(change, repo):
    report(repo, at("r/1", "warning", "a.py", 4))
    assert cli.build_parser().parse_args(["sast", "r.sarif", "--base", "x"]).min_level == "error"
    assert cli.main(["sast", "report.sarif", "--base", change]) == 0


def test_a_clean_report_passes(change, repo, capsys):
    report(repo)
    assert cli.main(["sast", "report.sarif", "--base", change]) == 0
    out = capsys.readouterr().out
    assert out.strip() == (
        "#### ✅ Static analysis: passed\n\n"
        "No findings on changed lines. Findings at level `error` or above block the merge."
    )


def test_findings_are_judged_across_reports(change, repo, capsys):
    report(repo, at("r/a", "error", "a.py", 4), name="python.sarif")
    report(repo, at("r/b", "warning", "a.py", 5), at("r/c", "error", "a.py", 1), name="go.sarif")
    assert cli.main(["sast", "python.sarif", "go.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert "**2 findings** on changed lines (1 error, 1 warning)." in out
    assert "1 pre-existing finding outside this change (not blocking)." in out
    assert cli.main(["sast", "go.sarif", "--base", change]) == 0


def test_absolute_and_file_uris_are_matched_to_the_change(change, repo, capsys):
    cwd = Path.cwd().as_posix()
    report(
        repo,
        at("r/abs", "error", f"{cwd}/a.py", 4),
        at("r/file", "warning", f"file://{cwd}/a.py", 5),
        at("r/base", "warning", "a.py", 4, base="%SRCROOT%"),
    )
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert "**3 findings** on changed lines (1 error, 2 warning)." in out


def test_annotations_are_repo_relative(repo, monkeypatch, capsys):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"svc/app.py": "a\nb\n"})
    monkeypatch.chdir(repo.root / "svc")
    report_path = write(repo.root / "svc", log(run(at("r/1", "error", "app.py", 2))), "r.sarif")
    assert cli.main(["sast", str(report_path), "--base", base]) == 1
    out = capsys.readouterr().out
    assert "::error file=svc/app.py,line=2::r/1: r/1 found" in out
    assert "- `app.py:2` **error** `r/1`: r/1 found" in out


def test_a_finding_about_the_whole_file_is_annotated_without_a_line(change, repo, capsys):
    report(repo, result("r/file", "error", locations=[location("a.py", None)]))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert "::error file=a.py::r/file: r/file found" in out
    assert "- `a.py` **error** `r/file`: r/file found" in out


def test_at_most_fifty_findings_are_annotated_blocking_ones_first(change, repo, capsys):
    notes = [at(f"n/{n}", "note", "a.py", 4) for n in range(40)]
    errors = [at(f"e/{n}", "error", "a.py", 5) for n in range(20)]
    report(repo, *notes, *errors)
    assert cli.main(["sast", "report.sarif", "--base", change, "--min-level", "warning"]) == 1
    out = capsys.readouterr().out
    assert out.count("::error file=") == 20
    assert out.count("::warning file=") == 30
    assert out.count("\n- `a.py:") == 50
    assert "- ... and 10 more" in out
    assert "**60 findings** on changed lines (20 error, 40 note)." in out


def test_more_than_fifty_blocking_findings_still_block(change, repo, capsys):
    report(repo, *(at(f"e/{n}", "error", "a.py", 4) for n in range(55)))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert out.count("::error file=") == 50
    assert "**55 findings**" in out


def test_the_verdict_goes_to_the_job_summary(change, repo, monkeypatch, capsys):
    summary = repo.root / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report(repo, at("r/1", "error", "a.py", 4))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    assert "#### ❌ Static analysis: failed" in summary.read_text()
    assert "- `a.py:4` **error** `r/1`" in summary.read_text()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            None,
            "::error::golden sast: SARIF report report.sarif not found; did the analysis tool run?",
        ),
        ("", "report.sarif is not valid JSON"),
        ("{not json", "report.sarif is not valid JSON"),
        ('{"version": "2.1.0"}', "report.sarif is not a SARIF log: it has no `runs` list"),
        ('["runs"]', "report.sarif is not a SARIF log"),
        (
            json.dumps(log(run({"ruleId": "R", "level": "error", "message": {"text": "m"}}))),
            "malformed SARIF report report.sarif: ValueError('a result has no physical location')",
        ),
    ],
)
def test_a_broken_report_is_exit_2_not_a_pass(change, repo, capsys, content, message):
    if content is not None:
        write(repo.root, content)
    assert cli.main(["sast", "report.sarif", "--base", change]) == 2
    out = capsys.readouterr().out
    assert message in out
    assert out.startswith("::error::golden sast: ")
    assert "Static analysis" not in out


def test_one_missing_report_among_several_is_exit_2(change, repo, capsys):
    report(repo, name="python.sarif")
    assert cli.main(["sast", "python.sarif", "go.sarif", "--base", change]) == 2
    assert "go.sarif not found" in capsys.readouterr().out


def test_an_unreadable_base_is_exit_2(change, repo, capsys):
    report(repo)
    assert cli.main(["sast", "report.sarif", "--base", "no-such-commit"]) == 2
    assert capsys.readouterr().out.startswith("::error::golden sast: ")


def test_the_command_needs_a_base_and_a_known_level(change, repo):
    report(repo)
    with pytest.raises(SystemExit) as no_base:
        cli.main(["sast", "report.sarif"])
    assert no_base.value.code == 2
    with pytest.raises(SystemExit) as bad_level:
        cli.main(["sast", "report.sarif", "--base", change, "--min-level", "none"])
    assert bad_level.value.code == 2


def commit_waiver(repo, base_branch="main"):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=20)
    repo.git("checkout", "-q", base_branch)
    repo.commit({"golden.toml": f'[waivers.sast]\nreason = "legacy module"\nexpires = {expires}\n'})
    repo.git("checkout", "-q", "feature")
    repo.git("rebase", "-q", base_branch)
    return repo.git("rev-parse", base_branch), expires


def test_a_merged_waiver_makes_the_gate_report_only(change, repo, capsys):
    base, expires = commit_waiver(repo)
    report(repo, at("py/sql", "error", "a.py", 4))
    assert cli.main(["sast", "report.sarif", "--base", base]) == 0
    out = capsys.readouterr().out
    assert f"::warning::sast is waived until {expires}: legacy module" in out
    assert "#### ⚠️ Static analysis: failed (waived)" in out
    assert "::error file=a.py,line=4::py/sql" in out


def test_a_waiver_the_change_adds_does_not_apply_to_itself(change, repo, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=20)
    repo.commit({"golden.toml": f'[waivers.sast]\nreason = "mine"\nexpires = {expires}\n'})
    report(repo, at("py/sql", "error", "a.py", 4))
    assert cli.main(["sast", "report.sarif", "--base", change]) == 1
    out = capsys.readouterr().out
    assert "waived" not in out.replace("waiver for sast applies after merge", "")


def test_the_waiver_does_not_hide_a_broken_report(change, repo, capsys):
    base, _ = commit_waiver(repo)
    write(repo.root, "{")
    assert cli.main(["sast", "report.sarif", "--base", base]) == 2
