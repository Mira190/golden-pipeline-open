"""Flaky-test scan: report parsing, aggregation across runs, rendering and the CLI."""

import json
import re
import subprocess
import sys

import pytest

from golden import cli, flakescan, plan
from golden.flakescan import Counts, Scan

CASE_BODY = {
    "pass": "",
    "fail": '<failure message="assert 0">trace</failure>',
    "error": '<error message="failed on setup">trace</error>',
    "skip": '<skipped type="pytest.skip" message="later"/>',
}


def exactly(message):
    """A `match` pattern that accepts nothing but `message`, so a reworded error is noticed."""
    return rf"\A{re.escape(message)}\Z"


def case(name, outcome="pass", classname="tests.test_x"):
    head = f'<testcase name="{name}"' + (f' classname="{classname}"' if classname else "")
    return f'{head} time="0.001">{CASE_BODY[outcome]}</testcase>'


def junit(cases):
    """A report in which `cases` maps a test name to its outcome."""
    body = "".join(case(name, outcome) for name, outcome in cases.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" tests="{len(cases)}">{body}</testsuite></testsuites>'
    )


def event(action, package="example.com/m/pkg", test=None, **extra):
    fields = {"Action": action, "Package": package, **({"Test": test} if test else {}), **extra}
    return json.dumps(fields)


def go_stream(*events):
    return "\n".join(events) + "\n"


def go_run(cases, package="example.com/m/pkg"):
    """A `go test -json` stream in which `cases` maps a test name to its outcome."""
    lines = []
    for name, outcome in cases.items():
        lines += [
            event("run", package, name),
            event("output", package, name, Output=f"--- {outcome.upper()}: {name}\n"),
            event(outcome, package, name, Elapsed=0),
        ]
    return go_stream(*lines)


def write_runs(tmp_path, reports, suffix=".xml"):
    paths = []
    for number, text in enumerate(reports):
        path = tmp_path / f"run-{number}{suffix}"
        path.write_text(text)
        paths.append(path)
    return paths


def junit_runs(tmp_path, *runs):
    return write_runs(tmp_path, [junit(cases) for cases in runs])


# ----------------------------------------------------------------- JUnit
def test_junit_outcomes_and_ids():
    text = junit({"test_a": "pass", "test_b": "fail", "test_c": "error", "test_d": "skip"})
    assert flakescan.PARSERS["junit"](text) == {
        "tests.test_x::test_a": "pass",
        "tests.test_x::test_b": "fail",
        "tests.test_x::test_c": "fail",
        "tests.test_x::test_d": "skip",
    }


def test_junit_test_without_classname_is_its_name():
    text = f"<testsuite>{case('plain', classname=None)}{case('empty', classname='')}</testsuite>"
    assert flakescan.PARSERS["junit"](text) == {"plain": "pass", "empty": "pass"}


def test_junit_vitest_report():
    text = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="3" failures="1" errors="0" time="0.01">
    <testsuite name="src/price.test.ts" tests="3" failures="1" errors="0" skipped="1">
        <testcase classname="src/price.test.ts" name="price &gt; rounds down" time="0.001">
        </testcase>
        <testcase classname="src/price.test.ts" name="price &gt; caps" time="0.002">
            <failure message="expected 1 to be 2" type="AssertionError">trace</failure>
        </testcase>
        <testcase classname="src/price.test.ts" name="price &gt; later" time="0">
            <skipped/>
        </testcase>
    </testsuite>
</testsuites>"""
    assert flakescan.PARSERS["junit"](text) == {
        "src/price.test.ts::price > rounds down": "pass",
        "src/price.test.ts::price > caps": "fail",
        "src/price.test.ts::price > later": "skip",
    }


@pytest.mark.parametrize(
    "wrap",
    [
        "<testsuite>{}</testsuite>",
        "<testsuites><testsuite>{}</testsuite></testsuites>",
        "<testsuites><testsuites><testsuite><testsuite>{}</testsuite></testsuite></testsuites>"
        "</testsuites>",
    ],
)
def test_junit_finds_cases_at_any_depth(wrap):
    text = wrap.format(case("a") + case("b", "fail"))
    assert flakescan.PARSERS["junit"](text) == {
        "tests.test_x::a": "pass",
        "tests.test_x::b": "fail",
    }


def test_junit_test_cases_from_several_suites_are_all_read():
    text = (
        f"<testsuites><testsuite>{case('a')}</testsuite>"
        f"<testsuite>{case('b', 'skip')}</testsuite></testsuites>"
    )
    assert set(flakescan.PARSERS["junit"](text)) == {"tests.test_x::a", "tests.test_x::b"}


def test_junit_failure_outranks_skip():
    text = '<testsuite><testcase name="a"><skipped/><failure/></testcase></testsuite>'
    assert flakescan.PARSERS["junit"](text) == {"a": "fail"}


@pytest.mark.parametrize("child", ["system-out", "properties", "rerunFailure"])
def test_junit_other_children_do_not_fail_a_test(child):
    text = f'<testsuite><testcase name="a"><{child}>x</{child}></testcase></testsuite>'
    assert flakescan.PARSERS["junit"](text) == {"a": "pass"}


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        (["pass", "fail"], "fail"),
        (["fail", "pass"], "fail"),
        (["skip", "pass"], "pass"),
        (["pass", "skip"], "pass"),
        (["skip", "fail"], "fail"),
        (["fail", "skip"], "fail"),
        (["skip", "skip"], "skip"),
        (["pass", "skip", "fail", "pass"], "fail"),
    ],
)
def test_a_test_reported_twice_in_one_run_keeps_its_worst_result(outcomes, expected):
    text = "<testsuite>" + "".join(case("dup", o) for o in outcomes) + "</testsuite>"
    assert flakescan.PARSERS["junit"](text) == {"tests.test_x::dup": expected}


@pytest.mark.parametrize("text", ["<testsuite><testcase", "not xml", "<a></b>"])
def test_junit_malformed_xml_is_an_error(text):
    with pytest.raises(ValueError, match="unreadable JUnit report"):
        flakescan.PARSERS["junit"](text)


@pytest.mark.parametrize("name", ["", None])
def test_junit_test_without_a_name_is_an_error(name):
    attrs = f' name="{name}"' if name is not None else ""
    with pytest.raises(ValueError, match=exactly("JUnit report has a test case without a name")):
        flakescan.PARSERS["junit"](f'<testsuite><testcase classname="c"{attrs}/></testsuite>')


def test_junit_report_without_test_cases_has_no_results():
    assert flakescan.PARSERS["junit"]("<testsuites/>") == {}


@pytest.fixture(scope="module")
def pytest_report(tmp_path_factory):
    """A report written by the real pytest, so the parser is held to its actual output."""
    root = tmp_path_factory.mktemp("real-pytest")
    (root / "test_sample.py").write_text(
        "import pytest\n\n"
        "class TestK:\n"
        "    def test_ok(self):\n        pass\n\n"
        "    def test_bad(self):\n        assert 0\n\n"
        "@pytest.mark.skip\ndef test_skipped():\n    pass\n\n"
        '@pytest.mark.parametrize("v", [1, 2])\ndef test_param(v):\n    pass\n\n'
        "@pytest.fixture\ndef boom():\n    raise RuntimeError\n\n"
        "def test_setup_error(boom):\n    pass\n\n"
        "@pytest.mark.xfail\ndef test_expected_failure():\n    assert 0\n"
    )
    report = root / "report.xml"
    argv = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "-c", "/dev/null"]
    subprocess.run(
        [sys.executable, *argv, f"--rootdir={root}", f"--junitxml={report}", str(root)],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return report


def test_the_real_pytest_report_is_understood(pytest_report):
    assert flakescan.read_run(pytest_report) == {
        "test_sample.TestK::test_ok": "pass",
        "test_sample.TestK::test_bad": "fail",
        "test_sample::test_skipped": "skip",
        "test_sample::test_param[1]": "pass",
        "test_sample::test_param[2]": "pass",
        "test_sample::test_setup_error": "fail",
        "test_sample::test_expected_failure": "skip",
    }


# ----------------------------------------------------------------- go test -json
# Trimmed from the real output of `go test -json ./...` (Go 1.24): one package with a pass,
# a failure, a skip and a failing subtest, one package that does not compile.
GO_REAL = go_stream(
    '{"ImportPath":"example.com/m/broken [example.com/m/broken.test]","Action":"build-output",'
    '"Output":"broken_test.go:5:28: undefined: undefinedThing\\n"}',
    '{"ImportPath":"example.com/m/broken [example.com/m/broken.test]","Action":"build-fail"}',
    '{"Time":"2026-10-22T09:05:25Z","Action":"start","Package":"example.com/m/broken"}',
    '{"Time":"2026-10-22T09:05:25Z","Action":"fail","Package":"example.com/m/broken",'
    '"Elapsed":0.001,"FailedBuild":"example.com/m/broken [example.com/m/broken.test]"}',
    '{"Time":"2026-10-22T09:05:25Z","Action":"start","Package":"example.com/m/ok"}',
    '{"Action":"run","Package":"example.com/m/ok","Test":"TestPass"}',
    '{"Action":"output","Package":"example.com/m/ok","Test":"TestPass","Output":"=== RUN\\n"}',
    '{"Action":"pass","Package":"example.com/m/ok","Test":"TestPass","Elapsed":0}',
    '{"Action":"fail","Package":"example.com/m/ok","Test":"TestFail","Elapsed":0}',
    '{"Action":"skip","Package":"example.com/m/ok","Test":"TestSkip","Elapsed":0}',
    '{"Action":"pass","Package":"example.com/m/ok","Test":"TestSub/good","Elapsed":0}',
    '{"Action":"fail","Package":"example.com/m/ok","Test":"TestSub/bad","Elapsed":0}',
    '{"Action":"fail","Package":"example.com/m/ok","Test":"TestSub","Elapsed":0}',
    '{"Action":"fail","Package":"example.com/m/ok","Elapsed":0.051}',
)


def test_the_real_go_stream_is_understood():
    assert flakescan.PARSERS["gotest-json"](GO_REAL) == {
        "example.com/m/ok/TestPass": "pass",
        "example.com/m/ok/TestFail": "fail",
        "example.com/m/ok/TestSkip": "skip",
        "example.com/m/ok/TestSub/good": "pass",
        "example.com/m/ok/TestSub/bad": "fail",
        "example.com/m/broken/(package)": "fail",
    }


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        # The parent fails because its subtest did: only the subtest is listed.
        (
            [("pass", "A/x"), ("fail", "A/y"), ("fail", "A")],
            {"p/A/x": "pass", "p/A/y": "fail"},
        ),
        # Two failing subtests share one parent, and the parent is listed once at most.
        (
            [("fail", "A/x"), ("fail", "A/y"), ("fail", "A")],
            {"p/A/x": "fail", "p/A/y": "fail"},
        ),
        # Nested subtests drop every failed ancestor, whatever the order of the events.
        (
            [("fail", "A"), ("fail", "A/b"), ("fail", "A/b/c"), ("pass", "A/b/d")],
            {"p/A/b/c": "fail", "p/A/b/d": "pass"},
        ),
        (
            [("fail", "A/b/c"), ("fail", "A/b"), ("fail", "A")],
            {"p/A/b/c": "fail"},
        ),
        # A parent that failed on its own (no failing subtest) stays.
        (
            [("pass", "A/x"), ("fail", "A")],
            {"p/A/x": "pass", "p/A": "fail"},
        ),
        # A passing parent stays, and a failed subtest drops nothing that is not its parent.
        (
            [("pass", "A/x"), ("pass", "A"), ("fail", "B/y"), ("pass", "B/z")],
            {"p/A/x": "pass", "p/A": "pass", "p/B/y": "fail", "p/B/z": "pass"},
        ),
        # Only a real parent is dropped, not a test whose name merely starts the same.
        (
            [("fail", "TestSub"), ("fail", "TestSubtle/x"), ("fail", "TestSu/y")],
            {"p/TestSub": "fail", "p/TestSubtle/x": "fail", "p/TestSu/y": "fail"},
        ),
        # Every failed ancestor goes, however deep the subtest is.
        (
            [("fail", "A/b/c/d"), ("fail", "A/b/c"), ("fail", "A/b"), ("fail", "A")],
            {"p/A/b/c/d": "fail"},
        ),
        (
            [("fail", "A/b/c/d"), ("fail", "A/b"), ("pass", "A/b/c")],
            {"p/A/b/c/d": "fail", "p/A/b/c": "pass"},
        ),
        # A failed grandchild drops the grandparent even when the parent never failed.
        (
            [("fail", "A"), ("pass", "A/b"), ("fail", "A/b/c")],
            {"p/A/b": "pass", "p/A/b/c": "fail"},
        ),
    ],
)
def test_go_failed_parent_of_a_failed_subtest_is_not_listed(events, expected):
    text = go_stream(*(event(action, "p", test) for action, test in events))
    assert flakescan.PARSERS["gotest-json"](text) == expected


def test_go_subtests_are_judged_per_package():
    text = go_stream(event("fail", "a", "A/x"), event("fail", "b", "A"))
    assert flakescan.PARSERS["gotest-json"](text) == {"a/A/x": "fail", "b/A": "fail"}


def test_go_failed_subtest_still_explains_its_package_failure():
    text = go_stream(event("fail", "a", "A/x"), event("fail", "a", "A"), event("fail", "a"))
    assert flakescan.PARSERS["gotest-json"](text) == {"a/A/x": "fail"}


def test_a_flaky_subtest_is_counted_once(tmp_path):
    runs = [
        go_stream(event("pass", "p", "A/x"), event("pass", "p", "A")),
        go_stream(event("fail", "p", "A/x"), event("fail", "p", "A")),
    ]
    result = flakescan.scan(write_runs(tmp_path, runs, ".json"))
    assert list(result.flaky) == ["p/A/x"]
    assert result.tests["p/A"] == Counts(passed=1)
    assert result.flaky_percent == 50


def test_go_ids_join_package_and_test():
    text = go_stream(event("pass", "example.com/m/a/b", "TestX"))
    assert flakescan.PARSERS["gotest-json"](text) == {"example.com/m/a/b/TestX": "pass"}


def test_go_output_lines_never_count_as_results():
    text = go_stream(
        event("output", test="TestA", Output="--- FAIL: TestA (0.00s)\n"),
        event("output", test="TestB", Output='{"Action":"fail"}\n'),
        event("pass", test="TestA"),
        event("pass", test="TestB"),
    )
    assert flakescan.PARSERS["gotest-json"](text) == {
        "example.com/m/pkg/TestA": "pass",
        "example.com/m/pkg/TestB": "pass",
    }


@pytest.mark.parametrize("action", ["start", "run", "pause", "cont", "output", "bench", "name"])
def test_go_other_actions_are_ignored(action):
    text = go_stream(event(action, test="TestA"), event("pass", test="TestB"))
    assert flakescan.PARSERS["gotest-json"](text) == {"example.com/m/pkg/TestB": "pass"}


@pytest.mark.parametrize("action", [None, 3, ["fail"], {"fail": 1}, ""])
def test_go_events_without_a_usable_action_are_ignored(action):
    text = go_stream(
        json.dumps({"Action": action, "Package": "p", "Test": "TestA"}),
        json.dumps({"Package": "p", "Test": "TestA"}),
        event("pass", "p", "TestB"),
    )
    assert flakescan.PARSERS["gotest-json"](text) == {"p/TestB": "pass"}


def test_go_blank_lines_are_skipped():
    text = "\n  \n" + event("pass", test="TestA") + "\n\n\t\n"
    assert flakescan.PARSERS["gotest-json"](text) == {"example.com/m/pkg/TestA": "pass"}


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [(["pass", "fail"], "fail"), (["skip", "pass"], "pass"), (["skip", "skip"], "skip")],
)
def test_go_test_reported_twice_keeps_its_worst_result(outcomes, expected):
    text = go_stream(*(event(o, test="TestA") for o in outcomes))
    assert flakescan.PARSERS["gotest-json"](text) == {"example.com/m/pkg/TestA": expected}


def test_go_package_level_results_are_not_tests():
    text = go_stream(
        event("pass", "p"),
        event("skip", "q", Elapsed=0),
        event("pass", "p", "TestA"),
        event("fail", "r", "TestB"),
        event("fail", "r"),
    )
    assert flakescan.PARSERS["gotest-json"](text) == {"p/TestA": "pass", "r/TestB": "fail"}


def test_go_package_that_fails_without_a_failing_test_is_a_failure():
    text = go_stream(
        event("pass", "a", "TestA"),
        event("fail", "a"),  # TestMain exits non-zero after every test passed
        event("fail", "b", "TestB"),
        event("fail", "b"),  # explained by TestB
        event("fail", "c"),  # does not compile
    )
    assert flakescan.PARSERS["gotest-json"](text) == {
        "a/TestA": "pass",
        "a/(package)": "fail",
        "b/TestB": "fail",
        "c/(package)": "fail",
    }


def test_go_failed_test_in_another_package_does_not_explain_a_package_failure():
    text = go_stream(event("fail", "a", "TestA"), event("fail", "b"))
    assert flakescan.PARSERS["gotest-json"](text) == {"a/TestA": "fail", "b/(package)": "fail"}


def test_go_passing_package_without_failures_adds_no_package_entry():
    text = go_stream(event("pass", "a", "TestA"), event("pass", "a"), event("skip", "a", "TestB"))
    assert flakescan.PARSERS["gotest-json"](text) == {"a/TestA": "pass", "a/TestB": "skip"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"Action":"pass"', "line 1 is not JSON"),
        ("# example.com/m\n", "line 1 is not JSON"),
        (event("pass", test="TestA") + "\nFAIL\texample.com/m\t0.1s\n", "line 2 is not JSON"),
        ("\n\n[1, 2]\n", "line 3 is not a JSON object"),
        ('"pass"', "line 1 is not a JSON object"),
        ("3", "line 1 is not a JSON object"),
        ("null", "line 1 is not a JSON object"),
    ],
)
def test_go_unparsable_stream_is_an_error(text, message):
    with pytest.raises(ValueError, match=message):
        flakescan.PARSERS["gotest-json"](text)


@pytest.mark.parametrize("outcome", ["pass", "fail", "skip"])
@pytest.mark.parametrize("package", [None, ""])
def test_go_result_without_a_package_is_an_error(outcome, package):
    line = json.dumps({"Action": outcome, "Package": package, "Test": "TestA"})
    with pytest.raises(ValueError, match=f"line 2: a {outcome} event has no package"):
        flakescan.PARSERS["gotest-json"](event("pass", test="TestB") + "\n" + line)


def test_go_event_without_a_package_is_fine_when_it_is_not_a_result():
    text = go_stream(json.dumps({"Action": "output", "Output": "x"}), event("pass", test="T"))
    assert flakescan.PARSERS["gotest-json"](text) == {"example.com/m/pkg/T": "pass"}


def test_go_stream_without_results_has_no_results():
    assert flakescan.PARSERS["gotest-json"](event("output", test="TestA")) == {}


# ----------------------------------------------------------------- detection and reading
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('<?xml version="1.0"?><testsuites/>', "junit"),
        ("<testsuite/>", "junit"),
        ("\n  \t<testsuite/>", "junit"),
        ('{"Action":"pass"}', "gotest-json"),
        ('\n\n  {"Action":"pass"}\n', "gotest-json"),
    ],
)
def test_detect_format(text, expected):
    assert flakescan.detect_format(text) == expected


@pytest.mark.parametrize("text", ["", " \n\t ", "\n"])
def test_detect_format_rejects_an_empty_report(text):
    with pytest.raises(ValueError, match=exactly("report is empty")):
        flakescan.detect_format(text)


@pytest.mark.parametrize("text", ["FAIL\texample.com/m", "[1]", "mode: set", "TOTAL 100%"])
def test_detect_format_rejects_unknown_reports(text):
    message = "unrecognised test report (expected JUnit XML or `go test -json` output)"
    with pytest.raises(ValueError, match=exactly(message)):
        flakescan.detect_format(text)


def test_read_run_detects_each_file_on_its_own(tmp_path):
    xml, stream = tmp_path / "report", tmp_path / "stream.txt"
    xml.write_text(junit({"a": "pass"}))
    stream.write_text(go_run({"TestA": "fail"}))
    assert flakescan.read_run(xml) == {"tests.test_x::a": "pass"}
    assert flakescan.read_run(stream) == {"example.com/m/pkg/TestA": "fail"}


def test_read_run_explicit_format_overrides_detection(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text(junit({"a": "pass"}))
    assert flakescan.read_run(path, "junit") == {"tests.test_x::a": "pass"}
    with pytest.raises(ValueError, match=r"report\.xml: line 1 is not JSON"):
        flakescan.read_run(path, "gotest-json")
    stream = tmp_path / "stream.json"
    stream.write_text(go_run({"TestA": "pass"}))
    with pytest.raises(ValueError, match=r"stream\.json: unreadable JUnit report"):
        flakescan.read_run(stream, "junit")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "report is empty"),
        ("garbage", "unrecognised test report"),
        ("<testsuite><testcase", "unreadable JUnit report"),
        ('{"Action": "pass"', "line 1 is not JSON"),
        ("<testsuites/>", "no tests found"),
        ('<testsuite><testsuite name="empty"/></testsuite>', "no tests found"),
        (event("output", test="TestA") + "\n" + event("pass", "p"), "no tests found"),
    ],
)
def test_read_run_fails_closed_and_names_the_file(tmp_path, content, message):
    path = tmp_path / "run-3.report"
    path.write_text(content)
    with pytest.raises(ValueError, match=re.escape(str(path)) + ": .*" + message):
        flakescan.read_run(path)


def test_read_run_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        flakescan.read_run(tmp_path / "absent.xml")


def test_read_run_non_utf8_report_is_an_error_naming_the_file(tmp_path):
    path = tmp_path / "latin1.xml"
    path.write_bytes(b'<testsuite><testcase name="caf\xe9"/></testsuite>')
    with pytest.raises(ValueError, match=r"latin1\.xml: .*codec"):
        flakescan.read_run(path)


# ----------------------------------------------------------------- aggregation
def test_counts_flaky_and_failing():
    assert Counts(passed=1, failed=1).flaky
    assert not Counts(passed=1, failed=1).failing
    assert Counts(failed=2).failing
    assert not Counts(failed=2).flaky
    assert Counts(failed=1, skipped=4).failing
    for stable in (Counts(passed=3), Counts(passed=1, skipped=2), Counts(skipped=3), Counts()):
        assert not stable.flaky
        assert not stable.failing


def test_scan_tells_flaky_from_failing_from_stable(tmp_path):
    runs = [
        {"stable": "pass", "flaky": "pass", "broken": "fail", "off": "skip", "mixed": "skip"},
        {"stable": "pass", "flaky": "fail", "broken": "fail", "off": "skip", "mixed": "pass"},
        {"stable": "pass", "flaky": "pass", "broken": "fail", "off": "skip", "mixed": "skip"},
        {"stable": "pass", "flaky": "fail", "broken": "fail", "off": "skip", "mixed": "skip"},
        {"stable": "pass", "flaky": "pass", "broken": "fail", "off": "skip", "mixed": "skip"},
    ]
    result = flakescan.scan(junit_runs(tmp_path, *runs))
    assert result.runs == 5
    assert result.tests == {
        "tests.test_x::broken": Counts(failed=5),
        "tests.test_x::flaky": Counts(passed=3, failed=2),
        "tests.test_x::mixed": Counts(passed=1, skipped=4),
        "tests.test_x::off": Counts(skipped=5),
        "tests.test_x::stable": Counts(passed=5),
    }
    assert list(result.flaky) == ["tests.test_x::flaky"]
    assert list(result.failing) == ["tests.test_x::broken"]
    assert not result.clean


def test_scan_a_test_that_fails_once_and_is_skipped_elsewhere_never_passed(tmp_path):
    result = flakescan.scan(junit_runs(tmp_path, {"a": "fail"}, {"a": "skip"}, {"a": "skip"}))
    assert not result.flaky
    assert result.failing == {"tests.test_x::a": Counts(failed=1, skipped=2)}


def test_scan_clean_when_every_test_is_stable(tmp_path):
    result = flakescan.scan(junit_runs(tmp_path, {"a": "pass", "b": "skip"}, {"a": "pass"}))
    assert result.clean
    assert result.flaky == {}
    assert result.failing == {}
    assert result.flaky_percent == 0


def test_scan_counts_a_test_only_where_it_was_seen(tmp_path):
    result = flakescan.scan(junit_runs(tmp_path, {"a": "pass", "b": "fail"}, {"a": "pass"}))
    assert result.tests == {
        "tests.test_x::a": Counts(passed=2),
        "tests.test_x::b": Counts(failed=1),
    }


def test_scan_reads_go_streams(tmp_path):
    reports = [
        go_run({"TestA": "pass", "TestB": "pass"}),
        go_run({"TestA": "pass", "TestB": "fail"}),
    ]
    result = flakescan.scan(write_runs(tmp_path, reports, ".json"))
    assert result.flaky == {"example.com/m/pkg/TestB": Counts(passed=1, failed=1)}


def test_scan_a_package_that_stops_compiling_in_one_run_is_a_failure(tmp_path):
    reports = [go_run({"TestA": "pass"}), go_stream(event("fail", "example.com/m/other"))]
    reports[1] += go_run({"TestA": "pass"})
    result = flakescan.scan(write_runs(tmp_path, reports, ".json"))
    assert result.failing == {"example.com/m/other/(package)": Counts(failed=1)}


def test_scan_hands_the_format_to_every_report(tmp_path):
    paths = write_runs(tmp_path, [junit({"a": "pass"}), junit({"a": "fail"})])
    assert flakescan.scan(paths, "junit").flaky
    with pytest.raises(ValueError, match="not JSON"):
        flakescan.scan(paths, "gotest-json")


@pytest.mark.parametrize("count", [0, 1])
def test_scan_needs_at_least_two_runs(tmp_path, count):
    paths = junit_runs(tmp_path, *[{"a": "pass"}] * count)
    with pytest.raises(ValueError, match=rf"at least 2 runs of the same commit, got {count}"):
        flakescan.scan(paths)


def test_scan_two_runs_are_enough(tmp_path):
    assert flakescan.scan(junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"})).runs == 2


def test_scan_one_bad_report_fails_the_scan(tmp_path):
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"})
    paths.append(tmp_path / "run-9.xml")
    with pytest.raises(OSError, match="run-9"):
        flakescan.scan(paths)
    paths[2].write_text("<testsuites/>")
    with pytest.raises(ValueError, match=r"run-9\.xml: no tests found"):
        flakescan.scan(paths)


@pytest.mark.parametrize(
    ("flaky", "tests", "percent"),
    [(0, 10, 0), (1, 3, 33.33), (2, 3, 66.67), (1, 8, 12.5), (4, 4, 100)],
)
def test_flaky_percent(flaky, tests, percent):
    counts = {f"t{n}": Counts(passed=1, failed=1 if n < flaky else 0) for n in range(tests)}
    assert Scan(3, counts).flaky_percent == percent


def test_flaky_percent_of_nothing_is_zero():
    assert Scan(3, {}).flaky_percent == 0.0


def test_always_failing_tests_are_not_in_the_flaky_rate():
    result = Scan(2, {"a": Counts(failed=2), "b": Counts(passed=2)})
    assert result.flaky_percent == 0
    assert not result.clean


# ----------------------------------------------------------------- rendering
def fake_tests(flaky=0, failing=0, stable=0):
    tests = {f"flaky-{n:03}": Counts(passed=1, failed=1) for n in range(flaky)}
    tests |= {f"failing-{n:03}": Counts(failed=2) for n in range(failing)}
    return tests | {f"stable-{n:03}": Counts(passed=2) for n in range(stable)}


def test_render_clean_scan():
    assert flakescan.render(Scan(5, fake_tests(stable=4))) == (
        "#### ✅ Flaky-test scan: no flaky tests\n\n"
        "5 runs of the same commit, 4 tests seen. Flaky rate **0%** (0 / 4)."
    )


def test_render_flaky_and_failing_tests_exactly():
    tests = {
        "tests.test_x::stable": Counts(passed=5),
        "tests.test_x::flaky": Counts(passed=3, failed=1, skipped=1),
        "tests.test_x::broken": Counts(failed=4, skipped=1),
        "tests.test_x::wobbly[a|b]": Counts(passed=1, failed=4),
    }
    assert flakescan.render(Scan(5, tests)) == (
        "#### ❌ Flaky-test scan: 2 flaky, 1 always failing\n\n"
        "5 runs of the same commit, 4 tests seen. Flaky rate **50%** (2 / 4).\n\n"
        "**Flaky** (passed and failed on the same commit)\n\n"
        "| Test | Passed | Failed | Skipped |\n"
        "|---|---|---|---|\n"
        "| `tests.test_x::flaky` | 3 | 1 | 1 |\n"
        "| `tests.test_x::wobbly[a\\|b]` | 1 | 4 | 0 |\n\n"
        "**Always failing** (never passed, so the default branch is red)\n\n"
        "| Test | Passed | Failed | Skipped |\n"
        "|---|---|---|---|\n"
        "| `tests.test_x::broken` | 0 | 4 | 1 |"
    )


def test_render_only_failing_tests_has_no_flaky_table():
    out = flakescan.render(Scan(2, fake_tests(failing=1, stable=1)))
    assert out.startswith("#### ❌ Flaky-test scan: 0 flaky, 1 always failing\n")
    assert "**Flaky**" not in out
    assert "| `failing-000` | 0 | 2 | 0 |" in out


def test_render_only_flaky_tests_has_no_failing_table():
    out = flakescan.render(Scan(2, fake_tests(flaky=1, stable=2)))
    assert out.startswith("#### ❌ Flaky-test scan: 1 flaky, 0 always failing\n")
    assert "Flaky rate **33.33%** (1 / 3)" in out
    assert "**Always failing**" not in out


def test_render_escapes_table_cells():
    out = flakescan.render(Scan(2, {"a|b\nc": Counts(passed=1, failed=1)}))
    assert "| `a\\|b c` | 1 | 1 | 0 |" in out


def test_render_lists_at_most_fifty_rows_per_table():
    out = flakescan.render(Scan(2, fake_tests(flaky=51, failing=50)))
    assert out.count("| `flaky-") == 50
    assert "`flaky-049`" in out
    assert "`flaky-050`" not in out
    assert out.count("| `flaky-049` | 1 | 1 | 0 |\n\n_and 1 more: see the JSON results._") == 1
    assert out.count("| `failing-") == 50
    assert "more" not in out.split("**Always failing**")[1]


def test_render_names_the_rows_not_shown():
    out = flakescan.render(Scan(2, fake_tests(flaky=50, failing=53)))
    assert "more" not in out.split("**Always failing**")[0]
    assert "_and 3 more: see the JSON results._" in out.split("**Always failing**")[1]


def test_findings_warn_per_flaky_test_and_fail_per_failing_one():
    tests = {
        "a::flaky": Counts(passed=3, failed=2),
        "a::broken": Counts(failed=5),
        "a::fine": Counts(passed=5),
    }
    assert flakescan.findings(Scan(5, tests)) == [
        ("warning", "flaky test a::flaky: passed 3, failed 2 of 5 runs"),
        ("error", "always failing test a::broken: failed 5 of 5 runs"),
    ]


def test_findings_of_a_clean_scan_are_empty():
    assert flakescan.findings(Scan(2, fake_tests(stable=3))) == []


def test_findings_are_capped_per_kind():
    found = flakescan.findings(Scan(2, fake_tests(flaky=60, failing=55)))
    assert [level for level, _ in found].count("warning") == 50
    assert [level for level, _ in found].count("error") == 50
    assert found[0][1].startswith("flaky test flaky-000")
    assert found[49][1].startswith("flaky test flaky-049")
    assert found[50][1].startswith("always failing test failing-000")
    assert found[-1][1].startswith("always failing test failing-049")


def test_as_dict_is_exact():
    tests = {
        "a::flaky": Counts(passed=3, failed=1, skipped=1),
        "b::broken": Counts(failed=5),
        "c::fine": Counts(passed=5),
        "d::fine": Counts(passed=5),
    }
    assert flakescan.as_dict(Scan(5, tests)) == {
        "runs": 5,
        "tests_seen": 4,
        "flaky_percent": 25.0,
        "flaky": [{"test": "a::flaky", "passed": 3, "failed": 1, "skipped": 1}],
        "always_failing": [{"test": "b::broken", "passed": 0, "failed": 5, "skipped": 0}],
    }


def test_as_dict_lists_every_test_not_just_the_first_fifty():
    data = flakescan.as_dict(Scan(2, fake_tests(flaky=60, failing=60)))
    assert len(data["flaky"]) == 60
    assert len(data["always_failing"]) == 60


# ----------------------------------------------------------------- default commands
def test_report_commands_cover_every_language():
    assert set(flakescan.REPORT_COMMANDS) == set(plan.DEFAULTS)


@pytest.mark.parametrize(
    ("language", "shuffle", "format_flag"),
    [
        ("python", "-p randomly", "--junitxml="),
        ("node", "--sequence.shuffle", "--reporter=junit"),
        ("go", "-shuffle=on", "-json"),
    ],
)
def test_report_commands_shuffle_and_write_the_report_file(language, shuffle, format_flag):
    command = flakescan.REPORT_COMMANDS[language]
    assert shuffle in command
    assert format_flag in command
    assert '"$GOLDEN_REPORT"' in command


def test_python_command_pins_the_same_randomiser_as_the_merge_gates():
    pinned = re.search(r"pytest-randomly==[\d.]+", plan.PY_TEST_PLUGINS)
    assert pinned
    assert f"--with {pinned.group()} " in flakescan.REPORT_COMMANDS["python"]


def test_go_command_runs_each_test_once_per_report():
    assert "-count=1" in flakescan.REPORT_COMMANDS["go"]


# ----------------------------------------------------------------- CLI
def test_cli_stable_suite_exits_0(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "pass", "b": "skip"}, {"a": "pass", "b": "skip"})
    assert cli.main(["flaky-scan", *map(str, paths)]) == 0
    out = capsys.readouterr().out
    assert out == (
        "#### ✅ Flaky-test scan: no flaky tests\n\n"
        "2 runs of the same commit, 2 tests seen. Flaky rate **0%** (0 / 2).\n"
    )


def test_cli_flaky_test_exits_1_with_a_warning(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "pass", "b": "pass"}, {"a": "fail", "b": "pass"})
    assert cli.main(["flaky-scan", *map(str, paths)]) == 1
    out = capsys.readouterr().out
    assert "::warning::flaky test tests.test_x::a: passed 1, failed 1 of 2 runs\n" in out
    assert "#### ❌ Flaky-test scan: 1 flaky, 0 always failing" in out
    assert "Flaky rate **50%** (1 / 2)" in out
    assert "::error" not in out


def test_cli_always_failing_test_exits_1_with_an_error(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "fail", "b": "pass"}, {"a": "error", "b": "pass"})
    assert cli.main(["flaky-scan", *map(str, paths)]) == 1
    out = capsys.readouterr().out
    assert "::error::always failing test tests.test_x::a: failed 2 of 2 runs\n" in out
    assert "#### ❌ Flaky-test scan: 0 flaky, 1 always failing" in out
    assert "::warning" not in out


def test_cli_annotates_before_the_summary(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "fail"})
    cli.main(["flaky-scan", *map(str, paths)])
    out = capsys.readouterr().out
    assert out.index("::warning::") < out.index("#### ")


def test_cli_reads_go_streams(tmp_path, capsys):
    paths = write_runs(tmp_path, [go_run({"TestA": "pass"}), go_run({"TestA": "fail"})], ".json")
    assert cli.main(["flaky-scan", *map(str, paths)]) == 1
    assert "::warning::flaky test example.com/m/pkg/TestA: passed 1, failed 1" in (
        capsys.readouterr().out
    )


def test_cli_appends_the_summary_to_the_job_summary(tmp_path, monkeypatch):
    target = tmp_path / "summary.md"
    target.write_text("earlier\n\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"})
    assert cli.main(["flaky-scan", *map(str, paths)]) == 0
    text = target.read_text()
    assert text.startswith("earlier\n\n#### ✅ Flaky-test scan: no flaky tests\n")
    assert text.endswith("(0 / 1).\n\n")


def test_cli_writes_json_results(tmp_path):
    paths = junit_runs(tmp_path, {"a": "pass", "b": "fail"}, {"a": "fail", "b": "fail"})
    out = tmp_path / "results.json"
    assert cli.main(["flaky-scan", "--json", str(out), *map(str, paths)]) == 1
    raw = out.read_text()
    assert raw.endswith("}\n")
    assert raw.startswith('{\n  "runs": 2,')
    assert json.loads(raw) == {
        "runs": 2,
        "tests_seen": 2,
        "flaky_percent": 50.0,
        "flaky": [{"test": "tests.test_x::a", "passed": 1, "failed": 1, "skipped": 0}],
        "always_failing": [{"test": "tests.test_x::b", "passed": 0, "failed": 2, "skipped": 0}],
    }


def test_cli_writes_json_for_a_clean_scan_too(tmp_path):
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"})
    out = tmp_path / "results.json"
    assert cli.main(["flaky-scan", "--json", str(out), *map(str, paths)]) == 0
    assert json.loads(out.read_text())["flaky"] == []


def test_cli_without_json_writes_no_file(tmp_path):
    cli.main(["flaky-scan", *map(str, junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"}))])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run-0.xml", "run-1.xml"]


def test_cli_format_flag(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"})
    assert cli.main(["flaky-scan", "--format", "junit", *map(str, paths)]) == 0
    assert cli.main(["flaky-scan", "--format", "auto", *map(str, paths)]) == 0
    assert cli.main(["flaky-scan", "--format", "gotest-json", *map(str, paths)]) == 2
    assert "golden flaky-scan: " in capsys.readouterr().out


def test_cli_rejects_an_unknown_format(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["flaky-scan", "--format", "tap", "x"])


def test_cli_without_reports_is_an_error(capsys):
    assert cli.main(["flaky-scan"]) == 2
    assert (
        "::error::golden flaky-scan: need test reports from at least 2 runs of the same commit, "
        "got 0\n" in capsys.readouterr().out
    )


def test_cli_one_report_is_an_error(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "fail"})
    assert cli.main(["flaky-scan", *map(str, paths)]) == 2
    assert "got 1" in capsys.readouterr().out


def test_cli_missing_report_is_an_error(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "pass"})
    assert cli.main(["flaky-scan", *map(str, paths), str(tmp_path / "absent.xml")]) == 2
    out = capsys.readouterr().out
    assert out.startswith("::error::golden flaky-scan: ")
    assert "absent.xml" in out


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("", "report is empty"),
        ("<testsuite><testcase", "unreadable JUnit report"),
        ("<testsuites/>", "no tests found"),
        ("hello", "unrecognised test report"),
    ],
)
def test_cli_unusable_report_is_an_error(tmp_path, capsys, bad, message):
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "fail"})
    paths[1].write_text(bad)
    assert cli.main(["flaky-scan", *map(str, paths)]) == 2
    out = capsys.readouterr().out
    assert "run-1.xml: " in out
    assert message in out
    assert "Flaky-test scan" not in out


def test_cli_unwritable_json_path_is_an_error(tmp_path, capsys):
    paths = junit_runs(tmp_path, {"a": "pass"}, {"a": "pass"})
    assert (
        cli.main(["flaky-scan", "--json", str(tmp_path / "no" / "dir.json"), *map(str, paths)]) == 2
    )
    assert "::error::golden flaky-scan: " in capsys.readouterr().out


@pytest.mark.parametrize("language", sorted(plan.DEFAULTS))
def test_cli_print_command(language, capsys):
    assert cli.main(["flaky-scan", "--print-command", language]) == 0
    assert capsys.readouterr().out == flakescan.REPORT_COMMANDS[language] + "\n"


def test_cli_print_command_scans_nothing(tmp_path, capsys):
    assert cli.main(["flaky-scan", "--print-command", "go", str(tmp_path / "absent.xml")]) == 0
    assert capsys.readouterr().out == flakescan.REPORT_COMMANDS["go"] + "\n"


def test_cli_print_command_rejects_an_unknown_language():
    with pytest.raises(SystemExit):
        cli.main(["flaky-scan", "--print-command", "cobol"])


def test_cli_help_names_the_argument_of_print_command(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["flaky-scan", "--help"])
    assert stop.value.code == 0
    assert "--print-command LANGUAGE" in capsys.readouterr().out
