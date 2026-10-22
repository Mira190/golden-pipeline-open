"""End-to-end: run the CLI against real git repositories."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from golden import cli

PRICING = "def total(a, b):\n    if a > b:\n        return a\n    return b\n"
COBERTURA = """<?xml version="1.0" ?>
<coverage line-rate="{rate}" lines-covered="{covered}" lines-valid="4" version="7">
<sources><source>{root}</source></sources>
<packages><package name="pkg"><classes><class filename="pkg/pricing.py" name="pricing.py">
<lines>{lines}</lines></class></classes></package></packages></coverage>"""


def cobertura(repo, covered_lines):
    lines = "".join(
        f'<line number="{n}" hits="{1 if n in covered_lines else 0}"/>' for n in range(1, 5)
    )
    (repo.root / "coverage.xml").write_text(
        COBERTURA.format(
            rate=len(covered_lines) / 4, covered=len(covered_lines), root=repo.root, lines=lines
        )
    )


@pytest.fixture
def feature(repo):
    """main has a README; the feature branch adds pkg/pricing.py."""
    repo.commit({"README.md": "x"}, "init")
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-qb", "feature")
    repo.commit({"pkg/__init__.py": "", "pkg/pricing.py": PRICING}, "feature")
    return base


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "golden 1.0.0" in capsys.readouterr().out


def test_presence_fails_without_tests(feature, capsys):
    assert cli.main(["presence", "--base", feature, "--preset", "python"]) == 1
    out = capsys.readouterr().out
    assert "::error file=pkg/pricing.py::missing tests: no test files changed" in out
    assert "Test presence: failed" in out


def test_presence_report_only(feature, capsys):
    assert cli.main(["presence", "--base", feature, "--preset", "python", "--report-only"]) == 0
    assert "failed (waived)" in capsys.readouterr().out


def test_presence_passes_with_tests(repo, feature):
    repo.commit({"tests/test_pricing.py": "from pkg.pricing import total\n"})
    assert cli.main(["presence", "--base", feature, "--preset", "python"]) == 0


def test_presence_waiver(repo, feature, capsys):
    (repo.root / "golden.toml").write_text(
        '[waivers.test-presence]\nreason = "spike"\nexpires = 2099-01-01\n'
    )
    assert cli.main(["presence", "--base", feature, "--preset", "python"]) == 2
    assert "at most 90 days" in capsys.readouterr().out


def test_changed(repo, feature, capsys):
    repo.commit(
        {"tests/test_pricing.py": "x", "golden.toml": '[presence]\nexempt = ["pkg/__init__.py"]\n'}
    )
    for kind, expected in [
        ("sources", "pkg/pricing.py"),
        ("tests", "tests/test_pricing.py"),
        ("mutation-targets", "pkg.pricing.x_total__mutmut_*"),
    ]:
        assert cli.main(["changed", "--base", feature, "--preset", "python", "--kind", kind]) == 0
        assert capsys.readouterr().out.strip() == expected


def test_presence_annotations_are_repo_relative(repo, monkeypatch, capsys):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"svc/pkg/mod.py": "x = 1\n"})
    monkeypatch.chdir(repo.root / "svc")
    assert cli.main(["presence", "--base", base, "--preset", "python"]) == 1
    out = capsys.readouterr().out
    assert "::error file=svc/pkg/mod.py::missing tests" in out
    assert "- `pkg/mod.py`: no test files changed" in out


def test_changed_in_subdirectory(repo, monkeypatch, capsys):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"svc/app/main.go": "package main", "other/x.go": "package x"})
    monkeypatch.chdir(repo.root / "svc")
    assert (
        cli.main(["changed", "--base", base, "--preset", "go", "--kind", "mutation-targets"]) == 0
    )
    assert capsys.readouterr().out.strip() == "./app"


def test_diff_coverage_fails_on_uncovered_lines(repo, feature, capsys):
    cobertura(repo, {1, 2})
    assert cli.main(["diff-coverage", "--base", feature, "coverage.xml"]) == 1
    out = capsys.readouterr().out
    assert "**50%** of 4 changed coverable lines covered (minimum 90%)" in out
    assert "::warning file=pkg/pricing.py,line=3::changed line not covered by tests" in out


def test_diff_coverage_passes(repo, feature, capsys):
    cobertura(repo, {1, 2, 3, 4})
    assert cli.main(["diff-coverage", "--base", feature, "coverage.xml"]) == 0
    assert "Diff coverage: passed" in capsys.readouterr().out


def test_diff_coverage_reports_tool_failure(repo, feature, capsys):
    (repo.root / "coverage.xml").write_text("not xml")
    assert cli.main(["diff-coverage", "--base", feature, "coverage.xml"]) == 1
    assert "diff-cover failed to run" in capsys.readouterr().out


def test_ratchet_cycle(repo, capsys):
    cobertura(repo, {1, 2, 3})
    assert cli.main(["ratchet", "coverage.xml", "--baseline", "base.json"]) == 0
    out = capsys.readouterr().out
    assert "no baseline yet" in out
    assert "::warning::coverage ratchet skipped: no default-branch baseline" in out
    assert cli.main(["ratchet", "coverage.xml", "--write-baseline", "base.json", "--sha", "s"]) == 0
    assert "baseline saved (75%)" in capsys.readouterr().out
    cobertura(repo, {1, 2})
    assert cli.main(["ratchet", "coverage.xml", "--baseline", "base.json"]) == 1
    assert "Δ -25 pp" in capsys.readouterr().out


def test_mutation_gate(repo, capsys):
    report = repo.root / "stats.json"
    report.write_text(json.dumps({"killed": 7, "survived": 3}))
    assert cli.main(["mutation", str(report), "--format", "mutmut"]) == 1
    report.write_text(json.dumps({"killed": 8, "survived": 2}))
    assert cli.main(["mutation", str(report), "--format", "mutmut"]) == 0
    assert "Score **80%**" in capsys.readouterr().out


def test_mutation_gate_lists_survivors(repo, capsys):
    report = repo.root / "mutation.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "a.ts": {
                        "mutants": [
                            {
                                "status": "Survived",
                                "mutatorName": "Eq",
                                "location": {"start": {"line": 3}},
                            }
                        ]
                    }
                }
            }
        )
    )
    assert cli.main(["mutation", str(report), "--format", "stryker"]) == 1
    out = capsys.readouterr().out
    assert "- `a.ts:3` Eq" in out
    assert "::warning file=a.ts,line=3::surviving mutant (Eq)" in out


def test_mutation_scoped_to_changed_lines(repo, capsys):
    repo.commit({"a.ts": "1\n2\n3\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"a.ts": "1\nTWO\n3\n"})
    mutants = [
        {"status": "Survived", "mutatorName": "Eq", "location": {"start": {"line": 1}}},
        {"status": "Killed", "mutatorName": "Eq", "location": {"start": {"line": 2}}},
    ]
    (repo.root / "m.json").write_text(json.dumps({"files": {"a.ts": {"mutants": mutants}}}))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 1
    assert cli.main(["mutation", "m.json", "--format", "stryker", "--base", base]) == 0
    assert "Score **100%**" in capsys.readouterr().out


def test_mutation_unreliable_run_fails(repo, capsys):
    mutants = [{"status": "Timeout", "location": {"start": {"line": n}}} for n in (1, 2, 3)]
    (repo.root / "m.json").write_text(json.dumps({"files": {"a.ts": {"mutants": mutants}}}))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 1
    assert "Most mutants timed out" in capsys.readouterr().out


def test_mutation_without_mutants(repo, capsys):
    (repo.root / "g.json").write_text('{"files": []}')
    assert cli.main(["mutation", "g.json", "--format", "gremlins"]) == 0
    assert "no mutants" in capsys.readouterr().out


def test_mutation_missing_report_is_an_error(repo, capsys):
    assert cli.main(["mutation", "missing.json", "--format", "stryker"]) == 2
    assert "did the mutation tool run" in capsys.readouterr().out


def test_policy_check_and_get(repo, capsys):
    (repo.root / "golden.toml").write_text(
        "[mutation]\nmin_score = 85\n"
        '[waivers.scorecard]\nreason = "new repo"\nexpires = 2026-11-01\n'
    )
    assert cli.main(["policy", "check"]) == 0
    out = capsys.readouterr().out
    assert "| `mutation.min_score` | 85 | 60 |" in out
    assert "**scorecard** until 2026-11-01: new repo" in out
    assert cli.main(["policy", "get", "flaky.repeats"]) == 0
    assert capsys.readouterr().out.strip() == "10"
    assert cli.main(["policy", "get", "coverage.ratchet_tolerance"]) == 0
    assert capsys.readouterr().out.strip() == "0.5"


def test_policy_get_requires_key():
    with pytest.raises(SystemExit):
        cli.main(["policy", "get"])


def test_invalid_policy_exits_2(repo, capsys):
    (repo.root / "golden.toml").write_text("[coverage]\ndiff_min = 10\n")
    assert cli.main(["policy", "check"]) == 2
    assert "::error::golden policy: coverage.diff_min = 10 is weaker" in capsys.readouterr().out


def test_git_error_exits_2(repo, capsys):
    repo.commit({"a": "b"})
    assert cli.main(["presence", "--base", "no-such-ref", "--preset", "go"]) == 2


def test_scorecard(repo, capsys, monkeypatch):
    monkeypatch.delenv("GOLDEN_DEFAULT_BRANCH", raising=False)
    assert cli.main(["scorecard", "--json", "score.json"]) == 1
    assert json.loads((repo.root / "score.json").read_text())["level"] == 0
    assert "Scorecard: failed" in capsys.readouterr().out


def test_dora(repo, capsys):
    log = repo.root / "deploys.jsonl"
    log.write_text(
        json.dumps(
            {
                "sha": "a",
                "committed_at": "2026-10-01T00:00:00+00:00",
                "deployed_at": "2026-10-01T02:00:00+00:00",
                "failed": False,
                "restored_at": None,
            }
        )
        + "\n\n"
    )
    assert cli.main(["dora", str(log)]) == 0
    assert json.loads(capsys.readouterr().out)["lead_time_hours_p50"] == 2.0


def test_summary_file(repo, monkeypatch, capsys):
    summary = repo.root / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    (repo.root / "stats.json").write_text("{}")
    assert cli.main(["mutation", "stats.json", "--format", "mutmut"]) == 0
    assert "no mutants" in summary.read_text()


def test_active_waiver_turns_failure_into_warning(repo, feature, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)
    (repo.root / "golden.toml").write_text(
        f'[waivers.test-presence]\nreason = "spike JIRA-1"\nexpires = {expires}\n'
    )
    assert cli.main(["presence", "--base", feature, "--preset", "python"]) == 0
    out = capsys.readouterr().out
    assert f"::warning::test-presence is waived until {expires}: spike JIRA-1" in out
    assert "failed (waived)" in out


def fake_diff_cover(monkeypatch, data, markdown="report"):
    """Replace the diff-cover subprocess with a canned JSON report."""

    def run(argv, **kwargs):
        fmt = next(a for a in argv if a.startswith("--format="))
        paths = dict(part.split(":", 1) for part in fmt.removeprefix("--format=").split(","))
        Path(paths["json"]).write_text(json.dumps(data))
        Path(paths["markdown"]).write_text(markdown)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", run)


@pytest.mark.parametrize(
    ("percent", "lines", "code"),
    [(90, 10, 0), (89.99, 10, 1), (0, 0, 0)],
)
def test_diff_coverage_threshold_is_inclusive(repo, monkeypatch, capsys, percent, lines, code):
    fake_diff_cover(
        monkeypatch, {"total_percent_covered": percent, "total_num_lines": lines, "src_stats": {}}
    )
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == code
    out = capsys.readouterr().out
    assert f"of {lines} changed coverable lines" in out
    assert "<details><summary>Report</summary>\n\nreport\n</details>" in out


def test_diff_coverage_caps_annotations_per_file(repo, monkeypatch, capsys):
    fake_diff_cover(
        monkeypatch,
        {
            "total_percent_covered": 0,
            "total_num_lines": 60,
            "src_stats": {"a.py": {"violation_lines": list(range(1, 61))}},
        },
    )
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == 1
    out = capsys.readouterr().out
    assert out.count("::warning file=a.py") == 50
    assert "line=50::" in out
    assert "line=51::" not in out


def test_diff_coverage_waiver(repo, monkeypatch, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=5)
    (repo.root / "golden.toml").write_text(
        f'[waivers.diff-coverage]\nreason = "legacy"\nexpires = {expires}\n'
    )
    fake_diff_cover(monkeypatch, {"total_percent_covered": 10, "total_num_lines": 10})
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == 0
    assert "diff-coverage is waived" in capsys.readouterr().out


def test_diff_coverage_failure_when_tool_exits_nonzero(repo, monkeypatch, capsys):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "out", "err")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == 1
    assert "#### ❌ Diff coverage: failed" in capsys.readouterr().out


def test_mutation_without_any_verdict_fails(repo, capsys):
    mutants = [{"status": "CompileError", "location": {"start": {"line": 1}}}]
    (repo.root / "m.json").write_text(json.dumps({"files": {"a.ts": {"mutants": mutants}}}))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 1
    assert "1 mutants in the changed code got no verdict" in capsys.readouterr().out


def test_interrupted_mutmut_run_is_an_error(repo, capsys):
    (repo.root / "s.json").write_text(json.dumps({"killed": 9, "check_was_interrupted_by_user": 2}))
    assert cli.main(["mutation", "s.json", "--format", "mutmut"]) == 2
    assert "interrupted" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["", "TN:\nend_of_record\n"])
def test_ratchet_rejects_a_report_without_coverable_lines(repo, capsys, content):
    (repo.root / "lcov.info").write_text(content)
    assert cli.main(["ratchet", "lcov.info", "--write-baseline", "base.json"]) == 2
    assert "no coverable lines" in capsys.readouterr().out
    assert not (repo.root / "base.json").exists()


def test_ratchet_rejects_a_malformed_baseline(repo, capsys):
    cobertura(repo, {1, 2, 3})
    (repo.root / "base.json").write_text('{"sha": "x"}')
    assert cli.main(["ratchet", "coverage.xml", "--baseline", "base.json"]) == 2
    assert "baseline base.json is malformed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "toml",
    [
        "coverage = 5\n",
        "[waivers]\nmutation = 'x'\n",
        "[waivers.mutation]\nreason = 'r'\nexpires = 2026-11-01T00:00:00\n",
        "[coverage]\ndiff_min = nan\n",
    ],
)
def test_malformed_policy_exits_2_without_traceback(repo, capsys, toml):
    (repo.root / "golden.toml").write_text(toml)
    assert cli.main(["policy", "check"]) == 2
    assert "::error::golden policy:" in capsys.readouterr().out


def test_diff_coverage_with_preset_passes_when_every_source_is_measured(repo, feature, capsys):
    cobertura(repo, {1, 2, 3, 4})
    assert cli.main(["diff-coverage", "--base", feature, "--preset", "python", "coverage.xml"]) == 0


def test_diff_coverage_fails_on_a_module_missing_from_the_report(repo, feature, capsys):
    # pytest-cov without `source` only reports imported modules: an untested
    # new module is absent from the report, and diff-cover alone calls it 100%.
    repo.commit({"pkg/untested.py": "def f():\n    return 1\n", "notes.md": "x\n"})
    cobertura(repo, {1, 2, 3, 4})
    assert cli.main(["diff-coverage", "--base", feature, "coverage.xml"]) == 0
    capsys.readouterr()
    assert cli.main(["diff-coverage", "--base", feature, "--preset", "python", "coverage.xml"]) == 1
    out = capsys.readouterr().out
    assert "::error file=pkg/untested.py::changed file is missing from the coverage report" in out
    assert "- `pkg/untested.py`" in out
    assert "notes.md" not in out.split("<details>")[0]


def test_diff_coverage_unmeasured_respects_exemptions(repo, feature, capsys):
    repo.commit({"gen/model.py": "X = 1\n"})
    (repo.root / "golden.toml").write_text('[presence]\nexempt = ["gen/**"]\n')
    cobertura(repo, {1, 2, 3, 4})
    assert cli.main(["diff-coverage", "--base", feature, "--preset", "python", "coverage.xml"]) == 0
