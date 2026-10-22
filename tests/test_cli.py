"""End-to-end: run the CLI against real git repositories."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from golden import cli, config
from golden.report import annotate

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
        # An exemption the change itself adds applies only after merge.
        ("sources", "pkg/__init__.py pkg/pricing.py"),
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


def test_active_waiver_turns_failure_into_warning(merged, repo, feature, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)
    (repo.root / "golden.toml").write_text(
        f'[waivers.test-presence]\nreason = "spike JIRA-1"\nexpires = {expires}\n'
    )
    assert cli.main(["presence", "--base", feature, "--preset", "python"]) == 0
    out = capsys.readouterr().out
    assert f"::warning::test-presence is waived until {expires}: spike JIRA-1" in out
    assert "failed (waived)" in out


@pytest.fixture
def merged(monkeypatch):
    """Treat the working-tree golden.toml as already merged on the base branch."""
    monkeypatch.setattr(
        config, "read_at", lambda base, path: path.read_text() if path.exists() else None
    )


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
def test_diff_coverage_threshold_is_inclusive(
    repo, merged, monkeypatch, capsys, percent, lines, code
):
    fake_diff_cover(
        monkeypatch, {"total_percent_covered": percent, "total_num_lines": lines, "src_stats": {}}
    )
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == code
    out = capsys.readouterr().out
    assert f"of {lines} changed coverable lines" in out
    assert "<details><summary>Report</summary>\n\nreport\n</details>" in out


def test_diff_coverage_caps_annotations_per_file(repo, merged, monkeypatch, capsys):
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


def test_diff_coverage_waiver(repo, merged, monkeypatch, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=5)
    (repo.root / "golden.toml").write_text(
        f'[waivers.diff-coverage]\nreason = "legacy"\nexpires = {expires}\n'
    )
    fake_diff_cover(monkeypatch, {"total_percent_covered": 10, "total_num_lines": 10})
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == 0
    assert "diff-coverage is waived" in capsys.readouterr().out


def test_diff_coverage_failure_when_tool_exits_nonzero(repo, merged, monkeypatch, capsys):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "out", "err")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["diff-coverage", "--base", "x", "coverage.xml"]) == 1
    assert "#### ❌ Diff coverage: failed" in capsys.readouterr().out


def test_annotation_properties_are_escaped(capsys):
    annotate("error", "50% done\nnext", file="a,b:c.py", line=3)
    assert capsys.readouterr().out == "::error file=a%2Cb%3Ac.py,line=3::50%25 done%0Anext\n"


def pull_request(repo, monkeypatch, base):
    event = repo.root / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": base}, "labels": []}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))


def test_policy_check_judges_a_pull_request_by_the_merged_policy(repo, monkeypatch, capsys):
    repo.commit({"golden.toml": "[coverage]\ndiff_min = 95\n"})
    pull_request(repo, monkeypatch, repo.git("rev-parse", "HEAD"))
    (repo.root / "golden.toml").write_text("[coverage]\ndiff_min = 85\n")
    assert cli.main(["policy", "check"]) == 0
    out = capsys.readouterr().out
    assert "| `coverage.diff_min` | 95 | 80 |" in out
    assert "::notice::golden.toml: coverage.diff_min = 85 applies after merge (now 95)" in out
    assert "**Not applied to this change**\n\n- coverage.diff_min = 85 applies" in out
    assert cli.main(["policy", "get", "coverage.diff_min"]) == 0
    assert capsys.readouterr().out.strip() == "95"


def test_policy_check_without_deferrals(repo, capsys):
    assert cli.main(["policy", "check"]) == 0
    out = capsys.readouterr().out
    assert "Not applied" not in out
    assert "::notice" not in out


def test_gates_use_the_policy_at_base(repo, capsys):
    repo.commit({"golden.toml": '[presence]\nexempt = ["gen/**"]\n'})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {
            "golden.toml": '[presence]\nexempt = ["gen/**", "pkg/**"]\n',
            "gen/x.py": "X = 1\n",
            "pkg/y.py": "Y = 1\n",
        }
    )
    assert cli.main(["presence", "--base", base, "--preset", "python"]) == 1
    out = capsys.readouterr().out
    assert "::error file=pkg/y.py::missing tests" in out
    assert "gen/x.py" not in out


def test_ratchet_reads_the_base_from_the_event(repo, monkeypatch, capsys):
    repo.commit({"golden.toml": "[coverage]\nratchet_tolerance = 0\n"})
    pull_request(repo, monkeypatch, repo.git("rev-parse", "HEAD"))
    (repo.root / "golden.toml").write_text("[coverage]\nratchet_tolerance = 2\n")
    (repo.root / "base.json").write_text('{"line_percent": 76}')
    cobertura(repo, {1, 2, 3})
    assert cli.main(["ratchet", "coverage.xml", "--baseline", "base.json"]) == 1
    assert "tolerance 0 pp" in capsys.readouterr().out


def test_unreadable_base_fails_closed(repo, monkeypatch, capsys):
    repo.commit({"README.md": "x"})
    pull_request(repo, monkeypatch, "0" * 40)
    assert cli.main(["policy", "check"]) == 2
    assert "cannot read the merged policy" in capsys.readouterr().out


@pytest.mark.parametrize(("content", "message"), [("{}", "is malformed"), ("[", "Expecting")])
def test_ratchet_corrupt_baseline_exits_2(repo, capsys, content, message):
    cobertura(repo, {1, 2, 3})
    (repo.root / "base.json").write_text(content)
    assert cli.main(["ratchet", "coverage.xml", "--baseline", "base.json"]) == 2
    assert message in capsys.readouterr().out


def test_mutation_accepts_the_elements_format(repo, capsys):
    report = {
        "files": {
            "a.cs": {
                "mutants": [
                    {"status": "Survived", "mutatorName": "M", "location": {"start": {"line": 2}}}
                ]
            }
        }
    }
    (repo.root / "m.json").write_text(json.dumps(report))
    assert cli.main(["mutation", "m.json", "--format", "mutation-testing-elements"]) == 1
    assert "- `a.cs:2` M" in capsys.readouterr().out


def test_scorecard_checks_ownership_of_the_policy_in_use(repo, capsys, monkeypatch):
    (repo.root / ".github").mkdir()
    (repo.root / ".github" / "CODEOWNERS").write_text("/.github/ @t\n/svc/golden.toml @t\n")
    (repo.root / "svc").mkdir()
    rules = [{"type": "pull_request", "parameters": {"require_code_owner_review": True}}]
    monkeypatch.setattr(cli.sc, "rules_from_env", lambda: rules)
    monkeypatch.chdir(repo.root / "svc")
    cli.main(["scorecard", "--root", str(repo.root)])
    row = "| L3 | Code owners must approve workflow and policy changes | ✅ |"
    assert row in capsys.readouterr().out


def test_scorecard_reads_tag_rulesets(repo, capsys, monkeypatch):
    tags = [
        {
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
        }
    ]
    monkeypatch.setattr(cli.sc, "tag_rulesets_from_env", lambda: tags)
    cli.main(["scorecard", "--root", str(repo.root)])
    assert "| L4 | Release tags can't be moved or deleted | ✅ |" in capsys.readouterr().out
    monkeypatch.setattr(cli.sc, "tag_rulesets_from_env", lambda: None)
    cli.main(["scorecard", "--root", str(repo.root)])
    assert "| L4 | Release tags can't be moved or deleted | ❔ unknown |" in capsys.readouterr().out


def test_dora_names_the_bad_line(repo, capsys):
    log = repo.root / "deploys.jsonl"
    log.write_text("{}\n")
    assert cli.main(["dora", str(log)]) == 2
    out = capsys.readouterr().out
    assert "::error::golden dora: line 1: missing committed_at, deployed_at, failed" in out


def test_unrelated_history_is_a_git_error(repo, capsys):
    # A shallow or orphaned checkout has no merge base: fail closed, never "no changes".
    repo.commit({"a.py": "x = 1\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "--orphan", "other")
    repo.commit({"b.py": "y = 1\n"})
    assert cli.main(["presence", "--base", base, "--preset", "python"]) == 2
    assert "::error::golden presence:" in capsys.readouterr().out


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


def test_diff_coverage_unmeasured_respects_exemptions(repo, merged, feature, capsys):
    repo.commit({"gen/model.py": "X = 1\n"})
    (repo.root / "golden.toml").write_text('[presence]\nexempt = ["gen/**"]\n')
    cobertura(repo, {1, 2, 3, 4})
    assert cli.main(["diff-coverage", "--base", feature, "--preset", "python", "coverage.xml"]) == 0


def test_local_plans_with_the_policy_at_the_merge_base(repo, capsys):
    repo.commit({"pkg/a.py": "def f():\n    return 1\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {"pkg/a.py": "def f():\n    return 2\n", "golden.toml": '[presence]\nexempt = ["pkg/**"]\n'}
    )
    argv = ["local", "--language", "python", "--base", base, "--mutation", "--dry-run"]
    assert cli.main(argv) == 0
    # The exemption the change adds is not applied to the change itself, as in CI.
    assert "GOLDEN_MUTATE='pkg.a.x_f__mutmut_*'" in capsys.readouterr().out


def test_policy_summary_is_exact(repo, monkeypatch, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)
    waiver = f'[waivers.scorecard]\nreason = "new repo"\nexpires = {expires}\n'
    repo.commit({"golden.toml": "[coverage]\ndiff_min = 95\n" + waiver})
    pull_request(repo, monkeypatch, repo.git("rev-parse", "HEAD"))
    head = (
        '[coverage]\ndiff_min = 85\n[waivers.mutation]\nreason = "x"\n' + f"expires = {expires}\n"
    )
    (repo.root / "golden.toml").write_text(head + waiver)
    summary = repo.root / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.main(["policy", "check"]) == 0
    assert summary.read_text() == (
        "### Golden policy\n\n| Key | Value | Floor |\n|---|---|---|\n"
        "| `coverage.diff_min` | 95 | 80 |\n"
        "| `coverage.ratchet_tolerance` | 0.5 | 2 |\n"
        "| `mutation.min_score` | 80 | 60 |\n"
        "| `flaky.repeats` | 10 | 3 |\n"
        "| `scorecard.min_level` | 2 | 1 |\n\n"
        f"**Waivers**\n\n- **scorecard** until {expires}: new repo\n\n"
        "**Not applied to this change**\n\n"
        "- coverage.diff_min = 85 applies after merge (now 95)\n"
        "- waiver for mutation applies after merge\n\n"
    )


def test_scorecard_json_and_verdict_are_exact(repo, monkeypatch, capsys):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)
    (repo.root / "golden.toml").write_text(
        f'[waivers.scorecard]\nreason = "new repo"\nexpires = {expires}\n'
    )
    assert cli.main(["scorecard", "--json", "score.json"]) == 0
    text = (repo.root / "score.json").read_text()
    assert text.startswith('{\n  "level": 0,\n  "controls": [\n    {\n      "id": "pr-ci",\n')
    out = capsys.readouterr().out
    assert "#### ⚠️ Scorecard: failed (waived)\n\nLevel 0 (minimum 2)." in out
    assert f"::warning::scorecard is waived until {expires}: new repo" in out


def test_plan_flags_changes_to_what_gates_the_change(repo, monkeypatch, capsys):
    repo.commit({".github/workflows/ci.yml": "on: pull_request\n", "a.py": "x = 1\n"})
    pull_request(repo, monkeypatch, repo.git("rev-parse", "HEAD"))
    repo.commit({".github/workflows/ci.yml": "on: push\n", "golden.toml": "[mutation]\n"})
    out_file = repo.root / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    assert cli.main(["plan", "--language", "python"]) == 0
    out = capsys.readouterr().out
    for path in (".github/workflows/ci.yml", "golden.toml"):
        assert f"::warning file={path}::This pull request changes how it is itself gated." in out
    assert "| gating-changes | `.github/workflows/ci.yml golden.toml` |" in out
    assert "gating-changes=.github/workflows/ci.yml golden.toml\n" in out_file.read_text()


def test_plan_without_gating_changes_warns_nothing(repo, monkeypatch, capsys):
    repo.commit({"a.py": "x = 1\n"})
    pull_request(repo, monkeypatch, repo.git("rev-parse", "HEAD"))
    repo.commit({"a.py": "x = 2\n"})
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert cli.main(["plan", "--language", "python"]) == 0
    out = capsys.readouterr().out
    assert "::warning" not in out
    assert json.loads(out)["gating-changes"] == ""


GO_SRC = "package p\n\nfunc Rate(q int) int {\n\tif q > 10 {\n\t\treturn 2\n\t}\n\treturn 1\n}\n"
GO_TEST = (
    "package p\n\nfunc TestRate(t *testing.T) {\n\tif Rate(11) != 2 {\n\t\tt.Fatal()\n\t}\n}\n"
)


def test_mutation_counts_test_touched_functions_with_preset(repo, capsys):
    repo.commit({"p/p.go": GO_SRC, "p/p_test.go": GO_TEST})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {
            "p/p_test.go": GO_TEST.replace(
                "\tif Rate(11) != 2 {\n\t\tt.Fatal()\n\t}\n", "\t_ = Rate(11)\n"
            )
        }
    )
    report = {
        "files": [
            {"file_name": "p/p.go", "mutations": [{"line": 4, "type": "CB", "status": "LIVED"}]}
        ]
    }
    (repo.root / "g.json").write_text(json.dumps(report))
    argv = ["mutation", "g.json", "--format", "gremlins", "--base", base]
    assert cli.main(argv) == 0  # changed lines only: the source did not change
    assert "no mutants in the changed code" in capsys.readouterr().out
    assert cli.main([*argv, "--preset", "go"]) == 1
    assert "- `p/p.go:4` CB" in capsys.readouterr().out


# ------------------------------------------------------- arid lines (logging/debug output)
LOGGY_TS = (
    "const total = a + b;\n"  # 1
    "console.log(total);\n"  # 2
    "if (total > 10) {\n"  # 3
    '  logger.info("big");\n'  # 4
    "}\n"  # 5
    "return total;\n"  # 6
)
ARID_NOTE = "mutants excluded as arid (logging/debug output)"


def stryker_report(repo, *mutants, name="src/a.ts", source=LOGGY_TS):
    """Write the source and a Stryker report: mutants are (line, status) pairs."""
    repo.write({name: source})
    entries = [
        {"status": s, "mutatorName": "Eq", "location": {"start": {"line": n}}} for n, s in mutants
    ]
    (repo.root / "m.json").write_text(json.dumps({"files": {name: {"mutants": entries}}}))


def test_mutation_excludes_arid_mutants_and_says_so(repo, capsys):
    stryker_report(repo, (1, "Killed"), (2, "Survived"), (4, "Survived"), (6, "Killed"))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 0
    out = capsys.readouterr().out
    assert "Mutation testing: passed" in out
    assert "Score **100%** (minimum 80%): 2 killed, 0 timed out, 0 survived, 0 not covered." in out
    assert f"2 {ARID_NOTE}." in out
    assert "surviving mutant" not in out


def test_mutation_arid_filter_can_be_switched_off(repo, capsys):
    stryker_report(repo, (1, "Killed"), (2, "Survived"), (4, "Survived"), (6, "Killed"))
    assert cli.main(["mutation", "m.json", "--format", "stryker", "--no-arid-filter"]) == 1
    out = capsys.readouterr().out
    assert "Score **50%**" in out
    assert ARID_NOTE not in out
    assert "- `src/a.ts:2` Eq" in out
    assert "- `src/a.ts:4` Eq" in out


def test_mutation_says_nothing_when_no_mutant_was_arid(repo, capsys):
    stryker_report(repo, (1, "Killed"), (6, "Survived"))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 1
    out = capsys.readouterr().out
    assert "Score **50%**" in out
    assert "arid" not in out


def test_mutation_only_arid_mutants_means_no_mutants_in_the_changed_code(repo, capsys):
    stryker_report(repo, (2, "Survived"), (4, "NoCoverage"))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 0
    out = capsys.readouterr().out
    assert "✅ Mutation testing: no mutants in the changed code" in out
    assert f"2 {ARID_NOTE}." in out
    assert "failed" not in out
    assert "::warning" not in out


def test_mutation_only_arid_mutants_without_a_verdict_is_not_inconclusive(repo, capsys):
    stryker_report(repo, (2, "CompileError"), (4, "Ignored"))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 0
    out = capsys.readouterr().out
    assert "no mutants in the changed code" in out
    assert "no verdict" not in out


def test_mutation_without_a_verdict_outside_arid_lines_still_fails(repo, capsys):
    stryker_report(repo, (2, "Survived"), (6, "CompileError"))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 1
    out = capsys.readouterr().out
    assert "1 mutants in the changed code got no verdict" in out
    assert f"1 {ARID_NOTE}." in out


def test_mutation_malformed_report_is_still_an_error_with_arid_lines(repo, capsys):
    repo.write({"src/a.ts": LOGGY_TS})
    mutants = [{"status": "Survived", "location": {"start": {"line": 2}}}, {"status": "Killed"}]
    (repo.root / "m.json").write_text(json.dumps({"files": {"src/a.ts": {"mutants": mutants}}}))
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 2
    assert "malformed stryker report" in capsys.readouterr().out


def test_mutation_unreadable_source_excludes_nothing(repo, capsys):
    stryker_report(repo, (2, "Survived"))
    (repo.root / "src" / "a.ts").unlink()
    assert cli.main(["mutation", "m.json", "--format", "stryker"]) == 1
    out = capsys.readouterr().out
    assert "- `src/a.ts:2` Eq" in out
    assert "arid" not in out


GO_LOGGY = (
    "package p\n\n"
    "func Rate(n int) int {\n"  # 3
    '\tlog.Printf("rate %d", n)\n'  # 4
    "\tif n > 10 {\n"  # 5
    "\t\treturn 2\n"  # 6
    "\t}\n"
    '\tfmt.Println("small")\n'  # 8
    '\t_ = fmt.Sprintf("%d", n+1)\n'  # 9
    "\treturn 1\n"
    "}\n"
)


def test_mutation_arid_filter_with_gremlins_and_a_base(repo, capsys):
    repo.commit({"p/p.go": "package p\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"p/p.go": GO_LOGGY})
    mutations = [
        {"line": line, "type": "ARITHMETIC_BASE", "status": status}
        for line, status in [
            (4, "LIVED"),
            (5, "KILLED"),
            (8, "NOT COVERED"),
            (9, "LIVED"),
            (99, "LIVED"),
        ]
    ]
    (repo.root / "g.json").write_text(
        json.dumps({"files": [{"file_name": "p/p.go", "mutations": mutations}]})
    )
    argv = ["mutation", "g.json", "--format", "gremlins", "--base", base]
    assert cli.main(argv) == 1
    out = capsys.readouterr().out
    assert "Score **50%**" in out  # lines 5 and 9; line 99 is outside the file
    assert f"2 {ARID_NOTE}." in out
    assert "- `p/p.go:9` ARITHMETIC_BASE" in out
    assert "p/p.go:4" not in out
    assert "p/p.go:8" not in out
    assert cli.main([*argv, "--no-arid-filter"]) == 1
    out = capsys.readouterr().out
    assert "Score **25%**" in out
    assert "arid" not in out


def test_mutation_arid_lines_outside_the_change_are_not_counted(repo, capsys):
    repo.commit({"src/a.ts": LOGGY_TS})
    base = repo.git("rev-parse", "HEAD")
    changed = LOGGY_TS.replace("a + b", "a - b")
    repo.commit({"src/a.ts": changed})
    stryker_report(repo, (1, "Killed"), (2, "Survived"), (4, "Survived"), source=changed)
    assert cli.main(["mutation", "m.json", "--format", "stryker", "--base", base]) == 0
    out = capsys.readouterr().out
    assert "Score **100%**" in out
    assert "arid" not in out


def test_mutation_arid_filter_is_on_unless_switched_off():
    parser = cli.build_parser()
    base = ["mutation", "m.json", "--format", "stryker"]
    assert parser.parse_args(base).arid_filter is True
    assert parser.parse_args([*base, "--no-arid-filter"]).arid_filter is False


def test_mutation_arid_filter_leaves_mutmut_stats_alone(repo, capsys):
    (repo.root / "stats.json").write_text(json.dumps({"killed": 8, "survived": 2}))
    assert cli.main(["mutation", "stats.json", "--format", "mutmut"]) == 0
    out = capsys.readouterr().out
    assert "Score **80%**" in out
    assert "arid" not in out


# ----------------------------------------- the mutation verdict text, pinned exactly
def mutants_report(root, statuses, name="a.ts"):
    entries = [
        {"status": s, "mutatorName": "Eq", "location": {"start": {"line": n}}}
        for n, s in enumerate(statuses, start=1)
    ]
    (root / "m.json").write_text(json.dumps({"files": {name: {"mutants": entries}}}))


def waive_mutation(repo):
    expires = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)
    (repo.root / "golden.toml").write_text(
        f'[waivers.mutation]\nreason = "known"\nexpires = {expires}\n'
    )


STRYKER = ["mutation", "m.json", "--format", "stryker"]


def test_mutation_no_verdict_text_is_exact(repo, capsys):
    mutants_report(repo.root, ["CompileError"])
    assert cli.main(STRYKER) == 1
    assert capsys.readouterr().out == (
        "#### ❌ Mutation testing: failed\n\n"
        "1 mutants in the changed code got no verdict (compile errors or an interrupted run), "
        "so the run proves nothing.\n"
    )


def test_mutation_no_verdict_can_be_waived(repo, capsys):
    mutants_report(repo.root, ["CompileError"])
    waive_mutation(repo)
    assert cli.main(STRYKER) == 0
    out = capsys.readouterr().out
    assert "Mutation testing: failed (waived)" in out
    assert "::warning::mutation is waived until" in out


def test_mutation_no_mutants_text_is_exact(repo, capsys):
    mutants_report(repo.root, [])
    assert cli.main(STRYKER) == 0
    assert capsys.readouterr().out == "#### ✅ Mutation testing: no mutants in the changed code\n"


def test_mutation_no_mutants_text_with_arid_mutants_is_exact(repo, capsys):
    repo.write({"a.ts": "console.log(1)\n"})
    mutants_report(repo.root, ["Survived"])
    assert cli.main(STRYKER) == 0
    assert capsys.readouterr().out == (
        "#### ✅ Mutation testing: no mutants in the changed code\n\n"
        "1 mutants excluded as arid (logging/debug output).\n"
    )


def test_mutation_unreliable_text_is_exact(repo, capsys):
    mutants_report(repo.root, ["Timeout", "Timeout", "Timeout"])
    assert cli.main(STRYKER) == 1
    assert capsys.readouterr().out == (
        "#### ❌ Mutation testing: failed\n\n"
        "Score **100%** (minimum 80%): 0 killed, 3 timed out, 0 survived, 0 not covered.\n\n"
        "Most mutants timed out, so the run proves nothing: raise the mutation tool's timeout "
        "(e.g. gremlins `--timeout-coefficient`).\n"
    )


def test_mutation_survivor_text_is_exact(repo, capsys):
    mutants_report(repo.root, ["Killed", "Survived", "NoCoverage"])
    assert cli.main(STRYKER) == 1
    assert capsys.readouterr().out == (
        "::warning file=a.ts,line=2::surviving mutant (Eq): no test noticed this change\n"
        "::warning file=a.ts,line=3::surviving mutant (Eq): no test noticed this change\n"
        "#### ❌ Mutation testing: failed\n\n"
        "Score **33.33%** (minimum 80%): 1 killed, 0 timed out, 1 survived, 1 not covered.\n\n"
        "Surviving mutants (tests did not notice these bugs):\n\n"
        "- `a.ts:2` Eq\n- `a.ts:3` Eq\n"
    )


def test_mutation_lists_and_annotates_at_most_fifty_survivors(repo, capsys):
    mutants_report(repo.root, ["Killed", *["Survived"] * 51])
    assert cli.main(STRYKER) == 1
    out = capsys.readouterr().out
    assert out.count("::warning ") == 50
    assert out.count("\n- `a.ts:") == 50
    assert "- `a.ts:51` Eq" in out
    assert "a.ts:52" not in out


def test_mutation_annotations_name_the_file_from_the_repository_root(repo, capsys, monkeypatch):
    (repo.root / "svc").mkdir()
    monkeypatch.chdir(repo.root / "svc")
    mutants_report(repo.root / "svc", ["Survived"])
    assert cli.main(STRYKER) == 1
    assert "::warning file=svc/a.ts,line=1::surviving mutant" in capsys.readouterr().out


def test_mutation_passing_run_does_not_ask_git_for_a_prefix(repo, capsys, monkeypatch):
    monkeypatch.setattr(cli.gitdiff, "repo_prefix", lambda: pytest.fail("repo_prefix called"))
    mutants_report(repo.root, ["Killed"])
    assert cli.main(STRYKER) == 0
    assert "Score **100%**" in capsys.readouterr().out


def test_mutation_failure_can_be_waived(repo, capsys):
    mutants_report(repo.root, ["Survived"])
    waive_mutation(repo)
    assert cli.main(STRYKER) == 0
    out = capsys.readouterr().out
    assert "Mutation testing: failed (waived)" in out
    assert "::warning::mutation is waived until" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["mutation", "m.json"],
        ["mutation", "m.json", "--format", "bogus"],
        ["mutation", "m.json", "--format", "stryker", "--preset", "bogus"],
    ],
)
def test_mutation_arguments_are_validated(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2
    capsys.readouterr()


def test_mutation_accepts_every_format_and_preset():
    parser = cli.build_parser()
    for fmt in ("gremlins", "mutation-testing-elements", "mutmut", "stryker"):
        assert parser.parse_args(["mutation", "m.json", "--format", fmt]).format == fmt
    args = parser.parse_args(["mutation", "m.json", "--format", "stryker", "--preset", "go"])
    assert args.preset == "go"


# ------------------------------------------------------------- the command line grammar
CHANGED = ["changed", "--base", "b", "--preset", "go", "--kind", "tests"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["policy"],
        ["policy", "bogus"],
        ["policy", "get", "no.such.key"],
        ["plan"],
        ["plan", "--language", "cobol"],
        ["changed", "--preset", "go", "--kind", "tests"],
        ["changed", "--base", "b", "--kind", "tests"],
        ["changed", "--base", "b", "--preset", "go"],
        ["changed", "--base", "b", "--preset", "bogus", "--kind", "tests"],
        ["changed", "--base", "b", "--preset", "go", "--kind", "bogus"],
        [*CHANGED, "--sep", "bogus"],
        ["presence", "--preset", "go"],
        ["presence", "--base", "b"],
        ["presence", "--base", "b", "--preset", "bogus"],
        ["diff-coverage", "r.xml"],
        ["diff-coverage", "--base", "b"],
        ["diff-coverage", "--base", "b", "--preset", "bogus", "r.xml"],
        ["ratchet"],
        ["ratchet", "r.xml", "--format", "bogus"],
    ],
)
def test_invalid_command_lines_are_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("sep", ["space", "newline", "comma"])
def test_changed_separators(sep):
    assert cli.build_parser().parse_args([*CHANGED, "--sep", sep]).sep == sep
    assert cli.build_parser().parse_args(CHANGED).sep == "space"


def test_command_line_defaults_and_types():
    parser = cli.build_parser()
    assert parser.prog == "golden"
    assert parser.description == cli.__doc__
    args = parser.parse_args(["plan", "--language", "python"])
    assert args.policy == Path("golden.toml")
    args = parser.parse_args(["--policy", "other.toml", "plan", "--language", "python"])
    assert args.policy == Path("other.toml")
    assert isinstance(args.policy, Path)
    assert parser.parse_args(["policy", "get", "coverage.diff_min"]).key == "coverage.diff_min"
    ratchet = parser.parse_args(["ratchet", "r.xml"])
    assert (ratchet.format, ratchet.sha, ratchet.report) == ("auto", "", Path("r.xml"))
    assert parser.parse_args(["ratchet", "r.xml", "--format", "auto"]).format == "auto"
    assert parser.parse_args(["diff-coverage", "--base", "b", "a.xml", "b.xml"]).report == [
        Path("a.xml"),
        Path("b.xml"),
    ]
