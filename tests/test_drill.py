"""scripts/drill.py without the language toolchains.

The shell steps (tests, mutation tools, gate commands) are recorded instead of
run; git and `golden changed` run for real, so the scratch repositories and the
mutation targets are the ones the drill builds in CI.
"""

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import ANY

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("drill", ROOT / "scripts" / "drill.py")
drill = importlib.util.module_from_spec(_spec)
sys.modules["drill"] = drill
_spec.loader.exec_module(drill)


@pytest.fixture
def shell(monkeypatch):
    """Record every shell step; `codes` maps a step's first golden subcommand to its exit code."""
    calls = []
    codes = {}

    def fake(cmd, cwd, *, env=None):
        calls.append((cmd, cwd, env))
        key = cmd[3] if isinstance(cmd, list) else "shell"
        return codes.get(key, 0)

    monkeypatch.setattr(drill, "sh", fake)
    return types.SimpleNamespace(calls=calls, codes=codes)


@pytest.mark.parametrize("language", ["python", "node", "go"])
def test_scenario_runs_each_gate_on_a_monorepo_copy(tmp_path, shell, language):
    assert drill.scenario(language, "strong", tmp_path) == dict.fromkeys(
        ("diff-coverage", "presence", "mutation"), True
    )
    cmds = drill.DEFAULTS[language]
    project = tmp_path / f"{language}-strong" / "services" / language
    shells = [c for c in shell.calls if isinstance(c[0], str)]
    gates = [c[0][3:] for c in shell.calls if isinstance(c[0], list)]
    assert [c[0] for c in shells] == [cmds["test-command"], cmds["mutation-command"]]
    assert {c[1] for c in shell.calls} == {project}
    assert gates == [
        ["diff-coverage", "--base", ANY, "--preset", language, cmds["coverage-report"]],
        ["presence", "--base", ANY, "--preset", language],
        ["mutation", cmds["mutation-report"], "--format", cmds["mutation-format"], "--base", ANY],
    ]
    base = gates[0][2]
    assert shells[1][2]["GOLDEN_BASE"] == base
    assert shells[1][2]["GOLDEN_MUTATE"]
    feature = drill.FEATURES[language]
    assert (project / feature.test_file).read_text() == feature.strong
    assert feature.code in (project / feature.source).read_text()


def test_scenario_mutation_targets_are_the_new_function(tmp_path, shell):
    drill.scenario("python", "weak", tmp_path)
    env = next(c[2] for c in shell.calls if c[2])
    assert env["GOLDEN_MUTATE"] == "pricing.core.x_bulk_discount_percent__mutmut_*"
    assert (tmp_path / "python-weak/services/python/tests/test_bulk.py").read_text() == (
        drill.FEATURES["python"].weak
    )


def test_scenario_without_tests_adds_no_test_file(tmp_path, shell):
    drill.scenario("go", "no-tests", tmp_path)
    assert not (tmp_path / "go-no-tests/services/go/pricing/bulk_test.go").exists()


def test_scenario_reports_each_verdict(tmp_path, shell):
    shell.codes.update({"diff-coverage": 1, "mutation": 1})
    assert drill.scenario("python", "weak", tmp_path) == {
        "diff-coverage": False,
        "presence": True,
        "mutation": False,
    }


@pytest.mark.parametrize(("failing", "message"), [(1, "tests failed to run")])
def test_scenario_fails_when_the_tests_cannot_run(tmp_path, shell, failing, message):
    shell.codes["shell"] = failing
    with pytest.raises(RuntimeError, match=f"python/no-tests: {message}"):
        drill.scenario("python", "no-tests", tmp_path)


def test_scenario_fails_when_the_mutation_tool_fails(tmp_path, monkeypatch):
    steps = iter([0, 0, 0, 1])
    monkeypatch.setattr(drill, "sh", lambda cmd, cwd, *, env=None: next(steps))
    with pytest.raises(RuntimeError, match="python/weak: mutation tool failed"):
        drill.scenario("python", "weak", tmp_path)


def test_node_scenario_reuses_installed_modules(tmp_path, shell, monkeypatch):
    example = tmp_path / "examples" / "node"
    (example / "node_modules" / "vitest").mkdir(parents=True)
    for name in ("package.json", "src/pricing.ts"):
        (example / name).parent.mkdir(parents=True, exist_ok=True)
        (example / name).write_text((ROOT / "examples" / "node" / name).read_text())
    monkeypatch.setattr(drill, "ROOT", tmp_path)
    drill.scenario("node", "weak", tmp_path / "work")
    linked = tmp_path / "work/node-weak/services/node/node_modules"
    assert linked.is_symlink()
    assert linked.resolve() == (example / "node_modules").resolve()


def test_sh_runs_commands_and_lists(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DRILL_VERBOSE", raising=False)
    assert drill.sh("exit 3", tmp_path) == 3
    assert drill.sh(["true"], tmp_path) == 0
    assert drill.sh('test "$X" = y', tmp_path, env={"X": "y"}) == 0
    assert drill.sh('test "$(echo *)" = "*"', tmp_path) == 0  # noglob, as in golden.yml
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("DRILL_VERBOSE", "1")
    drill.sh("echo out; echo err >&2", tmp_path)
    assert capsys.readouterr().out == "out\n\nerr\n\n"


def test_git_returns_stripped_output(tmp_path):
    drill.git(tmp_path, "init", "-q", "-b", "main")
    assert drill.git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
    with pytest.raises(subprocess.CalledProcessError):
        drill.git(tmp_path, "rev-parse", "no-such-ref")


def test_prepare_installs_node_modules_only_when_missing(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(drill, "ROOT", tmp_path)
    monkeypatch.setattr(
        drill, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: runs.append((a, kw)))
    )
    (tmp_path / "examples" / "node").mkdir(parents=True)
    drill.prepare("python")
    assert runs == []
    drill.prepare("node")
    assert runs == [
        (
            (["npm", "ci", "--ignore-scripts"],),
            {"cwd": tmp_path / "examples" / "node", "check": True},
        )
    ]
    (tmp_path / "examples" / "node" / "node_modules").mkdir()
    drill.prepare("node")
    assert len(runs) == 1


def test_main_reports_and_asserts_every_verdict(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(drill, "prepare", lambda language: None)
    monkeypatch.setattr(drill, "scenario", lambda language, name, workdir: drill.EXPECTED[name])
    assert drill.main(["go"]) == 0
    out = capsys.readouterr().out
    assert "| go | weak | pass | pass | **block** | ✅ |" in out
    assert json.loads(out.strip().splitlines()[-1]) == {"failures": []}
    assert summary.read_text().startswith("### Gate drills\n")


def test_main_fails_on_an_unexpected_verdict(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(drill, "prepare", lambda language: None)
    wrong = {"diff-coverage": True, "presence": True, "mutation": True}
    monkeypatch.setattr(drill, "scenario", lambda language, name, workdir: wrong)
    assert drill.main(["python"]) == 1
    out = capsys.readouterr().out
    assert "| python | weak | pass | pass | pass | ❌ |" in out
    assert "::error::python/no-tests: expected" in out
