"""The gate-effectiveness benchmark (evals/runner.py) as part of the test suite.

Every scenario must behave as labelled; a `known_gap` scenario is a strict xfail,
so fixing a gap fails the suite until the label is removed.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("evals_runner", ROOT / "evals" / "runner.py")
runner = importlib.util.module_from_spec(_spec)
sys.modules["evals_runner"] = runner
_spec.loader.exec_module(runner)

SCENARIOS = runner.load()


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(s, id=s.id, marks=[pytest.mark.xfail(reason=s.known_gap)] * bool(s.known_gap))
        for s in SCENARIOS
    ],
)
def test_scenario(scenario):
    result = runner.run(scenario)
    assert result.ok, result.detail


def test_corpus_is_labelled():
    assert len(SCENARIOS) >= 70
    for s in SCENARIOS:
        assert s.category in {"legit", "gaming", "robustness"}, s.id
        assert s.why, s.id
        checks = (s.expect, s.expect_lines, s.expect_json, s.expect_outputs, s.stdout_contains)
        assert any(c not in (None, []) for c in checks), s.id
    assert {s.category for s in SCENARIOS} == {"legit", "gaming", "robustness"}


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "x.toml"
    path.write_text(
        '[[scenario]]\nid = "a"\ngate = "g"\ncategory = "legit"\nwhy = "w"\nargv = []\n' * 2
    )
    with pytest.raises(ValueError, match="duplicate scenario ids"):
        runner.load([path])


def test_parse_outputs_handles_multiline_values():
    text = "a=1\nb<<EOF\nx\ny=2\nEOF\nc=3=4\n"
    assert runner.parse_outputs(text) == {"a": "1", "b": "x\ny=2", "c": "3=4"}


def result(category, ok, outcome="pass", gate="g", known_gap=""):
    s = runner.Scenario(
        id=f"{gate}/{category}/{ok}",
        gate=gate,
        category=category,
        why="w",
        argv=[],
        known_gap=known_gap,
    )
    return runner.Result(s, outcome, ok, "", 0.0)


def test_score_classifies_wrong_results_by_ground_truth():
    results = [
        result("gaming", ok=False),
        result("legit", ok=False, outcome="block"),
        result("robustness", ok=False, outcome="crash"),
        result("legit", ok=True),
        result("gaming", ok=False, known_gap="documented"),
    ]
    total = runner.score(results)["total"]
    assert total == {
        "scenarios": 5,
        "correct": 1,
        "escapes": 1,
        "false_blocks": 1,
        "robustness": 1,
        "crashes": 1,
        "known_gaps": 1,
    }


def test_failures_flag_fixed_known_gaps():
    assert runner.failures([result("gaming", ok=True, known_gap="x")]) == [
        "g/gaming/True: known gap now behaves as expected; drop `known_gap`"
    ]
    assert runner.failures([result("gaming", ok=False, known_gap="x")]) == []


def test_main_writes_report_and_json(tmp_path, capsys, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "eval.json"
    assert runner.main(["--json", str(out), "policy/typo-key", "presence/python-basic"]) == 0
    data = json.loads(out.read_text())
    assert [r["id"] for r in data["results"]] == ["policy/typo-key", "presence/python-basic"]
    assert data["score"]["total"]["correct"] == 2
    assert "| **total** | 2 | 2 | 0 | 0 | 0 | 0 | 0 |" in capsys.readouterr().out
    assert "### Gate eval" in summary.read_text()
