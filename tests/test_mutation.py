import json

import pytest

from golden import mutation


def write(tmp_path, data):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    return path


def stryker(*statuses):
    return {
        "schemaVersion": "3",
        "files": {
            "src/a.ts": {
                "mutants": [
                    {"status": s, "mutatorName": "Arith", "location": {"start": {"line": i + 1}}}
                    for i, s in enumerate(statuses)
                ]
            }
        },
    }


def gremlins(*statuses):
    return {
        "files": [
            {
                "file_name": "pricing/pricing.go",
                "mutations": [
                    {"line": i + 1, "type": "CONDITIONALS_BOUNDARY", "status": s}
                    for i, s in enumerate(statuses)
                ],
            }
        ]
    }


def test_score_definition():
    assert mutation.Tally(killed=6, timeout=2, survived=1, no_coverage=1).score == 80.0
    assert mutation.Tally().score is None


@pytest.mark.parametrize(
    ("tally", "unreliable"),
    [
        (mutation.Tally(killed=1, timeout=1), False),
        (mutation.Tally(killed=1, timeout=2), True),
        (mutation.Tally(), False),
    ],
)
def test_unreliable_when_most_mutants_time_out(tally, unreliable):
    assert tally.unreliable is unreliable


def test_mutmut(tmp_path):
    path = write(
        tmp_path,
        {
            "killed": 5,
            "timeout": 1,
            "suspicious": 1,
            "segfault": 1,
            "survived": 1,
            "no_tests": 1,
            "skipped": 9,
            "total": 19,
        },
    )
    tally, lived = mutation.evaluate(path, "mutmut", {"ignored.py": [(1, 1)]})
    assert tally == mutation.Tally(killed=5, timeout=3, survived=1, no_coverage=1)
    assert tally.score == 80.0
    assert lived == []


def test_stryker_excludes_invalid_mutants(tmp_path):
    path = write(
        tmp_path,
        stryker(
            "Killed",
            "Killed",
            "Timeout",
            "Survived",
            "NoCoverage",
            "CompileError",
            "RuntimeError",
            "Ignored",
            "Pending",
        ),
    )
    tally, lived = mutation.evaluate(path, "stryker")
    assert tally.score == 60.0
    assert lived == [
        mutation.Mutant("src/a.ts", 4, mutation.SURVIVED, "Arith"),
        mutation.Mutant("src/a.ts", 5, mutation.NO_COVERAGE, "Arith"),
    ]


def test_gremlins(tmp_path):
    path = write(
        tmp_path,
        gremlins("KILLED", "KILLED", "TIMED OUT", "LIVED", "NOT VIABLE", "NOT COVERED", "SKIPPED"),
    )
    tally, lived = mutation.evaluate(path, "gremlins")
    assert tally.score == 60.0
    assert [(m.line, m.status) for m in lived] == [(4, "survived"), (6, "no-coverage")]


@pytest.mark.parametrize(
    ("lines", "score"),
    [
        (None, 60.0),
        ({"pricing/pricing.go": [(1, 2)]}, 100.0),
        ({"pricing/pricing.go": [(4, 4), (6, 7)]}, 0.0),
        ({"pricing/pricing.go": [(5, 5)]}, None),
        ({"other.go": [(1, 9)]}, None),
    ],
)
def test_filter_to_changed_lines(tmp_path, lines, score):
    path = write(
        tmp_path,
        gremlins("KILLED", "KILLED", "TIMED OUT", "LIVED", "NOT VIABLE", "NOT COVERED", "SKIPPED"),
    )
    assert mutation.evaluate(path, "gremlins", lines)[0].score == score


@pytest.mark.parametrize("fmt", mutation.FORMATS)
def test_missing_report_fails_closed(tmp_path, fmt):
    with pytest.raises(FileNotFoundError, match="did the mutation tool run"):
        mutation.evaluate(tmp_path / "nope.json", fmt)


@pytest.mark.parametrize(
    ("fmt", "data"), [("mutmut", {}), ("stryker", {"files": {}}), ("gremlins", {"files": []})]
)
def test_empty_report_has_no_score(tmp_path, fmt, data):
    assert mutation.evaluate(write(tmp_path, data), fmt)[0].score is None
