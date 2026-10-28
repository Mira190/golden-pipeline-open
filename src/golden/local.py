"""`golden local`: the pipeline's verdict before you push (shift left).

Runs the same effective commands (`plan.commands`) and the same gate code as
`golden.yml`, against the merge-base with the default branch, so a developer or
a coding agent sees in seconds what CI would say in minutes. Nothing here is a
second implementation of a gate: every gate step re-invokes this CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from golden import gitdiff, plan
from golden.config import Policy

# Candidates for the default branch, most specific first.
BASE_REFS = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


@dataclass(frozen=True)
class Step:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Options:
    base: str
    policy_path: Path
    fast: bool = False  # policy, format, lint, types: fast enough for a pre-commit hook
    mutation: bool = False


@dataclass(frozen=True)
class Outcome:
    step: Step
    passed: bool
    seconds: float


def default_base(*, cwd: Path | None = None) -> str:
    """merge-base of HEAD with the default branch, as CI's pull_request base would be."""
    for ref in BASE_REFS:
        try:
            return gitdiff.merge_base(ref, cwd=cwd)
        except gitdiff.GitError:
            continue
    return ""


def _shell(command: str) -> list[str]:
    # Exactly how golden.yml runs a configured command.
    return ["bash", "-o", "noglob", "-euo", "pipefail", "-c", command]


def steps(language: str, inputs: dict[str, str], policy: Policy, opts: Options) -> list[Step]:
    """The pipeline's checks in CI order."""
    cmds = plan.commands(language, inputs, Path())
    base = opts.base
    golden = [sys.executable, "-m", "golden", "--policy", str(opts.policy_path)]
    out = [Step("policy", [*golden, "policy", "check"])]
    out += [
        Step(key.removesuffix("-command"), _shell(cmds[key]))
        for key in ("format-command", "lint-command", "typecheck-command")
        if cmds[key]
    ]
    # golden.yml runs golangci-lint through its action when no lint command is set.
    if language == "go" and not inputs.get("lint-command") and shutil.which("golangci-lint"):
        out.append(Step("golangci-lint", ["golangci-lint", "run"]))
    if opts.fast:
        return out
    out.append(Step("test", _shell(cmds["test-command"]), {"CI": "true"}))
    if not base:
        return out
    report = cmds["coverage-report"]
    out += [
        Step(
            "diff-coverage",
            [*golden, "diff-coverage", "--base", base, "--preset", language, report],
        ),
        Step("test-presence", [*golden, "presence", "--base", base, "--preset", language]),
    ]
    changes = plan.changes(language, base, policy)
    if changes["tests"]:
        env = {
            "GOLDEN_TESTS": changes["tests"],
            "GOLDEN_TEST_PACKAGES": changes["test-packages"],
            "GOLDEN_REPEATS": f"{policy.get('flaky.repeats'):g}",
        }
        out.append(Step("flake", _shell(cmds["flaky-command"]), env))
    if opts.mutation and changes["mutate"]:
        env = {"GOLDEN_MUTATE": changes["mutate"], "GOLDEN_BASE": base}
        out += [
            Step("mutation-run", _shell(cmds["mutation-command"]), env),
            Step(
                "mutation",
                [
                    *golden,
                    "mutation",
                    cmds["mutation-report"],
                    "--format",
                    cmds["mutation-format"],
                    "--base",
                    base,
                    "--preset",
                    language,
                ],
            ),
        ]
    return out


def run(plan_steps: list[Step]) -> list[Outcome]:
    """Run every step (not fail-fast: one round trip should surface every problem)."""
    outcomes = []
    for step in plan_steps:
        print(f"\n━━ golden local: {step.name}", flush=True)
        start = time.monotonic()
        code = subprocess.run(step.argv, env={**os.environ, **step.env}, check=False).returncode
        outcomes.append(Outcome(step, code == 0, round(time.monotonic() - start, 1)))
    return outcomes


DIRTY = (
    "uncommitted changes: the diff gates (coverage, presence, flake, mutation) only see "
    "committed work, as CI does; commit, then run again"
)


def render(outcomes: list[Outcome], base: str, *, dirty: bool = False) -> str:
    rows = [f"| {o.step.name} | {'✅' if o.passed else '❌'} | {o.seconds:g}s |" for o in outcomes]
    against = f"`{base[:12]}` (merge-base with the default branch)" if base else "no base found"
    note = "" if base else "\n\n_Diff gates skipped: fetch the default branch or pass `--base`._"
    if dirty and base:
        note += f"\n\n**⚠️ {DIRTY[0].upper()}{DIRTY[1:]}.**"
    return (
        "\n".join(
            [
                f"### Golden local, against {against}",
                "",
                "| Step | Result | Time |",
                "|---|---|---|",
                *rows,
            ]
        )
        + note
    )
