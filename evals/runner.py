"""Gate-effectiveness benchmark: replay labelled pull requests through the real gates.

Each scenario in `evals/scenarios/*.toml` describes a small repository, the change
a pull request makes to it, and what the gate *should* decide. The runner builds
the repository with git, simulates the GitHub `pull_request` event, runs the
`golden` CLI from this checkout and compares the outcome.

Outcomes are classified the way a merge sees them:

  pass   exit 0          the merge may proceed
  block  exit 1          a gate failed
  error  exit 2          the gate could not decide and failed closed (no traceback)
  crash  any traceback   never an acceptable outcome

and scored per gate as a confusion matrix: an *escape* is a change that should
have been stopped but passed; a *false block* is a legitimate change that was
stopped. Scenarios marked `known_gap` document limitations on purpose; they are
reported but do not fail the run, and one that starts passing is flagged so the
label is removed.

Run: uv run python evals/runner.py [--json out.json] [scenario-id-prefix ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "evals" / "scenarios"
EXIT = {0: "pass", 1: "block", 2: "error"}
# Environment the CLI would otherwise pick up from the machine running the eval
# (including mutmut's trampoline switch when the suite runs under mutation testing).
SCRUB = (
    "MUTANT_UNDER_TEST",
    "GITHUB_OUTPUT",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_REPOSITORY",
    "GITHUB_TOKEN",
    "GOLDEN_DEFAULT_BRANCH",
    "GOLDEN_INPUTS",
    "GIT_DIR",
    "GIT_WORK_TREE",
)


@dataclass
class Scenario:
    id: str
    gate: str
    category: str  # legit | gaming | robustness
    why: str
    argv: list[str]
    base: dict[str, str] = field(default_factory=dict)
    head: dict[str, str] = field(default_factory=dict)
    expect: str | None = None  # pass | block | error
    expect_lines: list[str] | None = None
    expect_json: dict[str, Any] | None = None
    expect_outputs: dict[str, str] | None = None
    stdout_contains: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    delete: list[str] = field(default_factory=list)
    workdir: str = "."
    labels: list[str] = field(default_factory=list)
    # Extra keys for the simulated event payload; `pull_request` is merged, not replaced.
    event: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] | None = None
    flake: int = 0  # >0: run pytest on the selected changed tests this many times
    known_gap: str = ""


@dataclass
class Result:
    scenario: Scenario
    outcome: str
    ok: bool
    detail: str
    seconds: float


def load(paths: list[Path] | None = None) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(paths or SCENARIOS.glob("*.toml")):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))["scenario"]
        scenarios.extend(Scenario(**item) for item in raw)
    ids = [s.id for s in scenarios]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate scenario ids: {sorted(duplicates)}")
    return scenarios


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _subst(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _write(root: Path, files: dict[str, str], values: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_subst(content, values), encoding="utf-8")


def build(s: Scenario, tmp: Path) -> tuple[Path, dict[str, str]]:
    """A repository with `base` on main and `head` on a pull-request branch."""
    repo = tmp / "repo"
    repo.mkdir()
    today = dt.datetime.now(dt.UTC).date()
    values = {
        "root": str(repo),
        "tmp": str(tmp),
        # Waiver dates relative to today, so scenarios never expire.
        "in30days": (today + dt.timedelta(days=30)).isoformat(),
        "in60days": (today + dt.timedelta(days=60)).isoformat(),
    }
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, s.base or {"README.md": "base\n"}, values)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    values["base"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "pull-request")
    _write(repo, s.head, values)
    for name in s.delete:
        (repo / name).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head", "--allow-empty")
    # Reports produced by CI steps exist in the workspace but are not part of the diff.
    _write(repo, s.artifacts, values)
    return repo, values


def _env(s: Scenario, tmp: Path, values: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in SCRUB and not k.startswith(("RUNNER_", "PYTEST_", "COV_CORE_", "MUTMUT_"))
    }
    labels = [{"name": n} for n in s.labels]
    event: dict[str, Any] = {**s.event}
    event["pull_request"] = {
        **s.event.get("pull_request", {}),
        "base": {"sha": values["base"]},
        "labels": labels,
    }
    (tmp / "event.json").write_text(json.dumps(event), encoding="utf-8")
    env |= {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(tmp / "event.json"),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if s.inputs is not None:
        env["GOLDEN_INPUTS"] = json.dumps(s.inputs)
    if s.expect_outputs is not None:
        env["GITHUB_OUTPUT"] = str(tmp / "github-output")
    return env


def parse_outputs(text: str) -> dict[str, str]:
    """Read a $GITHUB_OUTPUT file, including `key<<DELIMITER` multi-line values."""
    outputs: dict[str, str] = {}
    lines = iter(text.split("\n"))
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            for item in lines:
                if item == delimiter:
                    break
                body.append(item)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


def _flake(
    s: Scenario, cwd: Path, env: dict[str, str], tests: str
) -> subprocess.CompletedProcess[str]:
    """The flake gate as the workflow runs it: skipped with no tests, else N re-runs."""
    selected = tests.split()
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider"]
    argv += [f"--count={s.flake}", *selected]
    if not selected:
        return subprocess.CompletedProcess(argv, 0, "flake gate skipped: no changed tests", "")
    env = {**env, "PYTHONPATH": str(cwd)}
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def run(s: Scenario) -> Result:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="golden-eval-") as name:
        tmp = Path(name)
        repo, values = build(s, tmp)
        cwd = repo / s.workdir
        env = _env(s, tmp, values)
        argv = [_subst(arg, values) for arg in s.argv]
        proc = subprocess.run(
            [sys.executable, "-m", "golden", *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if s.flake and proc.returncode == 0:
            proc = _flake(s, cwd, env, proc.stdout)
        outputs = (
            parse_outputs(Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8"))
            if s.expect_outputs is not None and Path(env["GITHUB_OUTPUT"]).exists()
            else {}
        )
    outcome = (
        "crash"
        if "Traceback (most recent call last)" in proc.stderr
        else EXIT.get(proc.returncode, "block")
    )
    ok, detail = check(s, outcome, proc.stdout, outputs, values)
    if not ok:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
        detail += " | " + " / ".join(tail)
    return Result(s, outcome, ok, detail, time.monotonic() - started)


def check(
    s: Scenario, outcome: str, stdout: str, outputs: dict[str, str], values: dict[str, str]
) -> tuple[bool, str]:
    problems = []
    if s.expect is not None and outcome != s.expect:
        problems.append(f"expected {s.expect}, got {outcome}")
    if s.expect_lines is not None:
        got = sorted(line for line in stdout.splitlines() if line.strip())
        want = sorted(_subst(line, values) for line in s.expect_lines)
        if got != want:
            problems.append(f"expected lines {want}, got {got}")
    if s.expect_json is not None:
        try:
            parsed = json.loads(stdout)
        except ValueError:
            parsed = None
        data = parsed if isinstance(parsed, dict) else {}
        wrong = {k: data.get(k, "<missing>") for k, v in s.expect_json.items() if data.get(k) != v}
        if wrong:
            problems.append(f"json mismatch {wrong}")
    if s.expect_outputs is not None:
        wrong = {
            k: outputs.get(k, "<missing>")
            for k, v in s.expect_outputs.items()
            if outputs.get(k) != _subst(v, values)
        }
        if wrong:
            problems.append(f"outputs mismatch {wrong}")
    problems += [
        f"stdout lacks {needle!r}"
        for needle in s.stdout_contains
        if _subst(needle, values) not in stdout
    ]
    if s.expect is None and outcome == "crash":
        problems.append("crashed")
    return not problems, "; ".join(problems) or "as expected"


COLUMNS = ("scenarios", "correct", "escapes", "false_blocks", "robustness", "crashes", "known_gaps")


def score(results: list[Result]) -> dict[str, Any]:
    """Per-gate confusion counts, classified by each scenario's ground-truth category.

    A wrong result on a `gaming` scenario is an escape (the dodge got through), on a
    `legit` scenario a false block (a good PR stopped or mishandled), and on a
    `robustness` scenario a robustness failure. Known gaps are counted separately.
    """
    gates: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COLUMNS, 0))
    wrong_as = {"gaming": "escapes", "legit": "false_blocks", "robustness": "robustness"}
    for r in results:
        g = gates[r.scenario.gate]
        g["scenarios"] += 1
        g["correct"] += r.ok
        g["crashes"] += r.outcome == "crash"
        if r.scenario.known_gap:
            g["known_gaps"] += 1
        elif not r.ok:
            g[wrong_as[r.scenario.category]] += 1
    total = {k: sum(g[k] for g in gates.values()) for k in COLUMNS}
    return {"gates": dict(sorted(gates.items())), "total": total}


def failures(results: list[Result]) -> list[str]:
    out = []
    for r in results:
        if r.scenario.known_gap and r.ok:
            out.append(f"{r.scenario.id}: known gap now behaves as expected; drop `known_gap`")
        elif not r.scenario.known_gap and not r.ok:
            out.append(f"{r.scenario.id}: {r.detail}")
    return out


def render(results: list[Result]) -> str:
    summary = score(results)
    header = ("Gate", "Scenarios", "Correct", "Escapes", "False blocks", "Robustness", "Crashes")
    rows = [
        "| " + " | ".join([*header, "Known gaps"]) + " |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for gate, g in [*summary["gates"].items(), ("**total**", summary["total"])]:
        rows.append(f"| {gate} | " + " | ".join(str(g[k]) for k in COLUMNS) + " |")
    detail = ["", "| Scenario | Category | Outcome | Result |", "|---|---|---|---|"]
    for r in results:
        mark = "✅" if r.ok else ("📌 known gap" if r.scenario.known_gap else "❌")
        detail.append(f"| `{r.scenario.id}` | {r.scenario.category} | {r.outcome} | {mark} |")
    return "\n".join(["### Gate eval", "", *rows, *detail])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("only", nargs="*", help="scenario id prefixes")
    parser.add_argument("--json", type=Path, help="write machine-readable results here")
    args = parser.parse_args(argv)
    if shutil.which("git") is None:
        print("git is required", file=sys.stderr)
        return 2
    scenarios = [s for s in load() if not args.only or s.id.startswith(tuple(args.only))]
    results = [run(s) for s in scenarios]
    report = render(results)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    problems = failures(results)
    for p in problems:
        print(f"::error::{p}")
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "score": score(results),
                    "results": [
                        {
                            "id": r.scenario.id,
                            "gate": r.scenario.gate,
                            "category": r.scenario.category,
                            "outcome": r.outcome,
                            "ok": r.ok,
                            "known_gap": r.scenario.known_gap,
                            "detail": r.detail,
                            "seconds": round(r.seconds, 2),
                        }
                        for r in results
                    ],
                    "failures": problems,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
