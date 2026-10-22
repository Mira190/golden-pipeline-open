import sys
from pathlib import Path

import pytest

from golden import cli, config, gitdiff, local

POLICY = config.from_mapping({})
TRUE = dict.fromkeys(
    ("format-command", "lint-command", "typecheck-command", "test-command"), "true"
)


def names(steps):
    return [s.name for s in steps]


def opts(base="", **kw):
    return local.Options(base, Path("golden.toml"), **kw)


def test_fast_stops_before_tests():
    steps = local.steps("python", {}, POLICY, opts("abc", fast=True))
    assert names(steps) == ["policy", "format", "lint", "typecheck"]
    assert steps[0].argv[:3] == [sys.executable, "-m", "golden"]
    assert steps[1].argv[:6] == ["bash", "-o", "noglob", "-euo", "pipefail", "-c"]


def test_without_base_only_runs_commands():
    steps = local.steps("node", {}, POLICY, opts())
    assert names(steps) == ["policy", "lint", "typecheck", "test"]
    assert steps[-1].env == {"CI": "true"}


def test_inputs_override_defaults_like_in_ci():
    steps = local.steps("python", {"lint-command": "make lint"}, POLICY, opts(fast=True))
    assert steps[2].argv[-1] == "make lint"


def test_full_run_mirrors_the_pipeline(repo):
    repo.commit({"src/pkg/a.py": "def f():\n    return 1\n"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit(
        {"src/pkg/a.py": "def f():\n    return 2\n", "tests/test_a.py": "from pkg.a import f\n"}
    )
    steps = local.steps("python", {}, POLICY, opts(base, mutation=True))
    assert names(steps) == [
        "policy",
        "format",
        "lint",
        "typecheck",
        "test",
        "diff-coverage",
        "test-presence",
        "flake",
        "mutation-run",
        "mutation",
    ]
    by_name = {s.name: s for s in steps}
    assert by_name["diff-coverage"].argv[-5:] == [
        "--base",
        base,
        "--preset",
        "python",
        "coverage.xml",
    ]
    assert by_name["flake"].env == {
        "GOLDEN_TESTS": "tests/test_a.py",
        "GOLDEN_TEST_PACKAGES": "./tests",
        "GOLDEN_REPEATS": "10",
    }
    assert by_name["mutation-run"].env == {
        "GOLDEN_MUTATE": "pkg.a.x_f__mutmut_*",
        "GOLDEN_BASE": base,
    }
    assert by_name["mutation"].argv[-4:] == ["--base", base, "--preset", "python"]


def test_no_flake_or_mutation_steps_without_changes(repo):
    repo.commit({"README.md": "x"})
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"README.md": "y"})
    assert (
        names(local.steps("python", {}, POLICY, opts(base, mutation=True)))[-1] == "test-presence"
    )


@pytest.mark.parametrize(
    ("which", "inputs", "expected"),
    [
        ("/bin/golangci-lint", {}, True),
        (None, {}, False),
        ("/bin/golangci-lint", {"lint-command": "make lint"}, False),
    ],
)
def test_go_runs_golangci_lint_when_available(monkeypatch, which, inputs, expected):
    monkeypatch.setattr(local.shutil, "which", lambda _: which)
    steps = local.steps("go", inputs, POLICY, opts(fast=True))
    assert ("golangci-lint" in names(steps)) is expected


def test_default_base_is_the_merge_base(repo):
    assert local.default_base() == ""
    first = repo.commit({"a": "1"})
    repo.git("checkout", "-qb", "feature")
    repo.commit({"b": "2"})
    assert local.default_base() == first


def test_run_reports_every_step_without_stopping(capsys):
    steps = [
        local.Step("bad", ["false"]),
        local.Step("good", ["sh", "-c", 'test "$X" = 1'], {"X": "1"}),
    ]
    outcomes = local.run(steps)
    assert [(o.step.name, o.passed) for o in outcomes] == [("bad", False), ("good", True)]
    assert "━━ golden local: good" in capsys.readouterr().out


def test_render():
    outcomes = [local.Outcome(local.Step("lint", []), passed=False, seconds=1.5)]
    text = local.render(outcomes, "0123456789abcdef")
    assert "against `0123456789ab`" in text
    assert "| lint | ❌ | 1.5s |" in text
    assert "Diff gates skipped" in local.render(outcomes, "")


def test_cli_local_dry_run(repo, capsys):
    assert cli.main(["local", "--language", "node", "--base", "", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "lint: bash -o noglob -euo pipefail -c npx --no-install biome ci ." in out
    assert "test: CI='true' bash" in out


@pytest.mark.parametrize(("lint", "code"), [("true", 0), ("false", 1)])
def test_cli_local_runs_and_returns_the_verdict(repo, capsys, lint, code):
    sets = [f"--set={k}={v}" for k, v in {**TRUE, "lint-command": lint}.items()]
    assert cli.main(["local", "--language", "python", "--base", "", *sets]) == code
    assert "### Golden local, against no base found" in capsys.readouterr().out


def test_cli_local_reads_inputs_like_ci(repo, monkeypatch, capsys):
    monkeypatch.setenv("GOLDEN_INPUTS", '{"lint-command": "echo from-inputs"}')
    assert cli.main(["local", "--language", "python", "--base", "", "--fast", "--dry-run"]) == 0
    assert "echo from-inputs" in capsys.readouterr().out


def test_cli_local_rejects_malformed_set(repo, capsys):
    assert cli.main(["local", "--language", "python", "--set", "oops", "--dry-run"]) == 2
    assert "--set expects KEY=VALUE" in capsys.readouterr().out


def test_render_warns_about_uncommitted_changes():
    outcomes = [local.Outcome(local.Step("test", []), passed=True, seconds=1)]
    assert "Uncommitted changes" in local.render(outcomes, "abc", dirty=True)
    assert "Uncommitted" not in local.render(outcomes, "abc")
    assert "Uncommitted" not in local.render(outcomes, "", dirty=True)


def test_cli_local_warns_when_the_diff_gates_cannot_see_the_work(repo, capsys):
    base = repo.commit({"a.py": "x = 1\n"})
    repo.write({"a.py": "x = 2\n"})
    sets = [f"--set={k}={v}" for k, v in {**TRUE, "test-command": "false"}.items()]
    assert cli.main(["local", "--language", "python", "--base", base, *sets]) == 1
    out = capsys.readouterr().out
    assert "::warning::uncommitted changes: the diff gates" in out
    assert "**⚠️ Uncommitted changes" in out


@pytest.mark.parametrize("change", [{"a.py": "z\n"}, {"new.py": "y\n"}])
def test_uncommitted_sees_edits_and_new_files_but_not_ignored_ones(repo, change):
    repo.commit({"a.py": "x\n", ".gitignore": "coverage.xml\n"})
    repo.write({"coverage.xml": "<coverage/>"})
    assert not gitdiff.uncommitted()
    repo.write(change)
    assert gitdiff.uncommitted()
