"""Flaky-test scan: tests that both pass and fail on the same commit.

The flake gate re-runs only the tests a pull request *changed*. Nothing else measures the
flakiness of the suite that already exists, so a flaky old test fails unrelated pull requests
and nobody knows which one it is. This module reads the test reports of N runs of one commit
(the nightly `scheduled.yml` workflow produces them) and tells flaky tests, which pass in some
runs and fail in others, from tests that fail every time, which mean the default branch is red.

Two report formats are understood, detected from the content:

* JUnit XML: pytest `--junitxml`, Vitest `--reporter=junit`. A test is `classname::name`;
  `<failure>` and `<error>` are a failure, `<skipped>` a skip.
* `go test -json` streams. A test is `package/TestName`; only `pass`, `fail` and `skip` events
  count and the `output` lines are ignored. A test that failed only because a subtest failed
  is not listed itself, and a package that failed without a failing test (build error) is a
  failure named `package/(package)`.

Fails closed, like every gate: a missing or unreadable report, a report with no tests in it,
or fewer than two runs is an error, never a clean scan.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path

# The reports are produced by our own test step in the same job; they are not untrusted input.
from xml.etree import ElementTree  # nosec B405

PASS, FAIL, SKIP = "pass", "fail", "skip"
# Within one report a failure outranks a pass and a pass outranks a skip.
RANK = {SKIP: 0, PASS: 1, FAIL: 2}
GO_ACTIONS = {"pass": PASS, "fail": FAIL, "skip": SKIP}
# One run cannot tell a flaky test from a failing one.
MIN_RUNS = 2
# Annotations and table rows per kind; the JSON results always list everything.
MAX_LISTED = 50

# How the nightly workflow produces one report per run, in random order. Kept here, not in
# plan.py: these are not the commands of the merge gates. They write to $GOLDEN_REPORT.
REPORT_COMMANDS: dict[str, str] = {
    "python": 'uv run --with pytest-randomly==5.0.0 pytest -p randomly --junitxml="$GOLDEN_REPORT"',
    "node": "npx --no-install vitest run --sequence.shuffle --reporter=junit "
    '--outputFile="$GOLDEN_REPORT"',
    "go": 'go test -shuffle=on -count=1 -json ./... > "$GOLDEN_REPORT"',
}


def _record(results: dict[str, str], test: str, outcome: str) -> None:
    results[test] = max(outcome, results.get(test, SKIP), key=RANK.__getitem__)


def _junit(text: str) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(text)  # noqa: S314  # nosec B314
    except ElementTree.ParseError as exc:
        raise ValueError(f"unreadable JUnit report: {exc}") from exc
    results: dict[str, str] = {}
    # iter() finds test cases at any depth: `<testsuites>` may nest `<testsuite>`s.
    for case in root.iter("testcase"):
        name = case.get("name")
        if not name:
            raise ValueError("JUnit report has a test case without a name")
        children = {child.tag for child in case}
        if children & {"failure", "error"}:
            outcome = FAIL
        elif "skipped" in children:
            outcome = SKIP
        else:
            outcome = PASS
        classname = case.get("classname")
        _record(results, f"{classname}::{name}" if classname else name, outcome)
    return results


def _go_events(text: str) -> Iterator[tuple[str, str, str]]:
    """(outcome, package, test) of every `pass`, `fail` and `skip` event.

    `test` is empty for an event about a whole package.
    """
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number} is not JSON: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"line {number} is not a JSON object")
        outcome = GO_ACTIONS.get(str(event.get("Action")))
        if outcome is None:
            continue
        package = str(event.get("Package") or "")
        if not package:
            raise ValueError(f"line {number}: a {outcome} event has no package")
        yield outcome, package, str(event.get("Test") or "")


def _parents(test: str) -> Iterator[str]:
    """`A/b/c` -> `A/b`, `A`: the tests a subtest runs inside."""
    parent, slash, _ = test.rpartition("/")
    while slash:
        yield parent
        parent, slash, _ = parent.rpartition("/")


def _drop_derived_failures(results: dict[str, str], failed_tests: dict[str, set[str]]) -> None:
    """Go fails a test whose subtest failed. That failure is the subtest's: listing the parent
    too would count one flaky subtest twice (three times when nested)."""
    for package, tests in failed_tests.items():
        for test in tests:
            for parent in _parents(test):
                if parent in tests:
                    results.pop(f"{package}/{parent}", None)


def _gotest_json(text: str) -> dict[str, str]:
    results: dict[str, str] = {}
    failed_tests: dict[str, set[str]] = {}
    failed_packages: set[str] = set()
    for outcome, package, test in _go_events(text):
        if test:
            _record(results, f"{package}/{test}", outcome)
            if outcome == FAIL:
                failed_tests.setdefault(package, set()).add(test)
        elif outcome == FAIL:
            failed_packages.add(package)
    _drop_derived_failures(results, failed_tests)
    # A package that fails without any failing test (build error, TestMain, a panic outside a
    # test) would otherwise vanish from the scan and leave a red default branch looking clean.
    for package in failed_packages - failed_tests.keys():
        results[f"{package}/(package)"] = FAIL
    return results


PARSERS: dict[str, Callable[[str], dict[str, str]]] = {
    "junit": _junit,
    "gotest-json": _gotest_json,
}


def detect_format(text: str) -> str:
    start = text.lstrip()[:1]
    if not start:
        raise ValueError("report is empty")
    if start == "<":
        return "junit"
    if start == "{":
        return "gotest-json"
    raise ValueError("unrecognised test report (expected JUnit XML or `go test -json` output)")


def read_run(path: Path, fmt: str = "auto") -> dict[str, str]:
    """The outcome of every test in one run's report, keyed by test id."""
    try:
        text = path.read_text(encoding="utf-8")
        results = PARSERS[detect_format(text) if fmt == "auto" else fmt](text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not results:
        raise ValueError(f"{path}: no tests found; did the test run produce a report?")
    return results


@dataclass(frozen=True)
class Counts:
    """In how many runs a test passed, failed and was skipped."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def flaky(self) -> bool:
        return self.passed > 0 and self.failed > 0

    @property
    def failing(self) -> bool:
        """Failed whenever it ran: it never passed."""
        return self.failed > 0 and self.passed == 0


@dataclass(frozen=True)
class Scan:
    runs: int
    tests: dict[str, Counts]

    @property
    def flaky(self) -> dict[str, Counts]:
        return {test: c for test, c in self.tests.items() if c.flaky}

    @property
    def failing(self) -> dict[str, Counts]:
        return {test: c for test, c in self.tests.items() if c.failing}

    @property
    def flaky_percent(self) -> float:
        if not self.tests:
            return 0.0
        return round(100 * len(self.flaky) / len(self.tests), 2)

    @property
    def clean(self) -> bool:
        return not self.flaky and not self.failing


def scan(paths: Sequence[Path], fmt: str = "auto") -> Scan:
    """Combine the reports of several runs of the same commit."""
    if len(paths) < MIN_RUNS:
        raise ValueError(
            f"need test reports from at least {MIN_RUNS} runs of the same commit, got {len(paths)}"
        )
    tally: dict[str, Counter[str]] = {}
    for path in paths:
        for test, outcome in read_run(path, fmt).items():
            tally.setdefault(test, Counter())[outcome] += 1
    return Scan(
        len(paths),
        {test: Counts(c[PASS], c[FAIL], c[SKIP]) for test, c in sorted(tally.items())},
    )


def _cell(test: str) -> str:
    return test.replace("|", "\\|").replace("\n", " ")


def _table(rows: dict[str, Counts]) -> list[str]:
    lines = ["| Test | Passed | Failed | Skipped |", "|---|---|---|---|"]
    lines += [
        f"| `{_cell(test)}` | {c.passed} | {c.failed} | {c.skipped} |"
        for test, c in islice(rows.items(), MAX_LISTED)
    ]
    if len(rows) > MAX_LISTED:
        lines += ["", f"_and {len(rows) - MAX_LISTED} more: see the JSON results._"]
    return lines


def render(result: Scan) -> str:
    flaky, failing = result.flaky, result.failing
    if result.clean:
        title = "#### ✅ Flaky-test scan: no flaky tests"
    else:
        title = f"#### ❌ Flaky-test scan: {len(flaky)} flaky, {len(failing)} always failing"
    lines = [
        title,
        "",
        f"{result.runs} runs of the same commit, {len(result.tests)} tests seen. "
        f"Flaky rate **{result.flaky_percent:g}%** ({len(flaky)} / {len(result.tests)}).",
    ]
    if flaky:
        lines += ["", "**Flaky** (passed and failed on the same commit)", "", *_table(flaky)]
    if failing:
        lines += [
            "",
            "**Always failing** (never passed, so the default branch is red)",
            "",
            *_table(failing),
        ]
    return "\n".join(lines)


def findings(result: Scan) -> list[tuple[str, str]]:
    """(level, message) of the workflow annotations: a warning per flaky test, an error per
    test that always fails."""
    flaky = [
        (
            "warning",
            f"flaky test {test}: passed {c.passed}, failed {c.failed} of {result.runs} runs",
        )
        for test, c in islice(result.flaky.items(), MAX_LISTED)
    ]
    failing = [
        ("error", f"always failing test {test}: failed {c.failed} of {result.runs} runs")
        for test, c in islice(result.failing.items(), MAX_LISTED)
    ]
    return flaky + failing


def as_dict(result: Scan) -> dict[str, object]:
    """The machine-readable results (`--json`)."""
    return {
        "runs": result.runs,
        "tests_seen": len(result.tests),
        "flaky_percent": result.flaky_percent,
        "flaky": [{"test": test, **asdict(c)} for test, c in result.flaky.items()],
        "always_failing": [{"test": test, **asdict(c)} for test, c in result.failing.items()],
    }
