"""Real-fault benchmark: would the Golden gates have flagged weak tests where a real bug lived?

For every bug in `manifest.toml` (a real fix commit F whose parent is P) the runner

1. verifies the regression test fails on P (with F's test files) and passes on F;
2. at P, with the tests that existed then, measures coverage of the faulty lines, runs mutmut on
   the buggy function(s) with the Golden Pipeline's selector scheme, and scores the run with
   `golden mutation` (the CLI gate itself);
3. repeats the mutation run at F with F's tests (the control: the bar is reachable).

    uv run python evals/realfaults/run.py                  # whole manifest -> results.json/.md
    uv run python evals/realfaults/run.py --only boltons-bits-length --workdir /tmp/rf

Needs network (git clones, PyPI), `git` and `uv`. It is not part of the pytest suite.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from golden import mutation, presets

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PINS = ("pytest==9.1.1", "coverage==7.16.2", "mutmut==3.8.0", "pytest-timeout==2.4.0")
EXCLUDE_NEWER = "2026-10-14T00:00:00Z"  # transitive dependencies resolve as of this date
VERIFY_TIMEOUT = 120  # seconds per test: a hang at P (infinite loop) counts as a failure
MUTMUT_ARGS = ("-p", "no:randomly", "-x", "-q")  # as in this repository's [tool.mutmut]
GATE_MIN_SCORE = 80  # platform default `mutation.min_score`
GATE_DIFF_MIN = 90  # platform default `coverage.diff_min`
HOUR = 3600
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
MUTANT = re.compile(r"\.x(?:ǁ(?P<cls>[^ǁ]+)ǁ|_)(?P<fn>.+)__mutmut_(?P<n>\d+)$")
TEST_FAIL = re.compile(r"^(?:FAILED|ERROR|SUBFAILED\([^)]*\)) (\S+?::\S+)", re.MULTILINE)
SCORE = re.compile(r"Score \*\*([\d.]+)%\*\*")
Span = tuple[int, int]


# --------------------------------------------------------------------------- manifest
@dataclass(frozen=True)
class Project:
    name: str
    url: str
    python: str
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    requirements: tuple[str, ...] = ()
    also_copy: tuple[str, ...] = ()
    # What the project's CI hands to pytest for the coverage run (default: test_paths); lets
    # a project's doctests (`--doctest-modules` in pytest_extra) count as part of its suite.
    run_paths: tuple[str, ...] = ()
    # Tests that cannot pass in an uninstalled checkout (package-metadata checks).
    deselect: tuple[str, ...] = ()
    # Extra pytest arguments for every run (e.g. `--no-cov` for a plugin configured in addopts).
    pytest_extra: tuple[str, ...] = ()
    # Extra lines for the generated [mutmut] section (e.g. `process_isolation = forkserver`).
    mutmut_config: tuple[str, ...] = ()
    # HEAD the candidate list was taken from (documentation; not used by the runner).
    snapshot: str = ""


@dataclass(frozen=True)
class Bug:
    id: str
    project: str
    fix: str
    parent: str
    file: str
    functions: tuple[str, ...]
    regression_tests: tuple[str, ...]
    issue: str = ""
    summary: str = ""
    skip: str = ""  # reason this bug is not run (recorded in the results, excluded from metrics)


def _tuples(table: dict[str, Any]) -> dict[str, Any]:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in table.items()}


def load_manifest(path: Path) -> tuple[dict[str, Project], list[Bug]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    projects = {p["name"]: Project(**_tuples(p)) for p in data["project"]}
    bugs = [Bug(**_tuples(b)) for b in data["bug"]]
    unknown = {b.project for b in bugs} - set(projects)
    if unknown:
        raise SystemExit(f"manifest: bugs reference unknown projects {sorted(unknown)}")
    return projects, bugs


# --------------------------------------------------------------------------- processes
def clean_env(venv: Path | None = None) -> dict[str, str]:
    """The ambient environment without CI context, with the project venv first on PATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "VIRTUAL_ENV"))}
    env.pop("CI", None)
    env["PYTHONHASHSEED"] = "0"
    if venv:
        env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env['PATH']}"
    return env


def sh(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command and never raise on a non-zero exit."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env or clean_env(),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=4 * HOUR,
        check=False,
    )


def must(proc: subprocess.CompletedProcess[str], what: str) -> str:
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-1500:]
        raise RuntimeError(f"{what} failed (exit {proc.returncode}): {tail}")
    return proc.stdout


def git(cwd: Path, *args: str) -> str:
    return must(sh(["git", *args], cwd), f"git {' '.join(args[:2])}")


# --------------------------------------------------------------------------- workspace
@dataclass
class Workspace:
    root: Path
    project: Project

    @property
    def clone(self) -> Path:
        return self.root / "clones" / self.project.name

    @property
    def venv(self) -> Path:
        return self.root / "venvs" / self.project.name

    def tool(self, name: str) -> str:
        return str(self.venv / "bin" / name)

    def env(self) -> dict[str, str]:
        return clean_env(self.venv)

    def extra_args(self) -> list[str]:
        """The project's deselects and extra arguments, for pytest and mutmut alike."""
        return [f"--deselect={d}" for d in self.project.deselect] + list(self.project.pytest_extra)

    def pytest_args(self) -> list[str]:
        """Arguments every pytest run gets: fixed order, no cache, the project's extras."""
        return ["-q", "-p", "no:cacheprovider", "-p", "no:randomly", *self.extra_args()]


def prepare(ws: Workspace) -> None:
    """Clone the project and build its virtualenv once."""
    if not ws.clone.exists():
        ws.clone.parent.mkdir(parents=True, exist_ok=True)
        clone = ["git", "clone", "-q", "--filter=blob:none", ws.project.url, str(ws.clone)]
        must(sh(clone, ws.root), f"clone {ws.project.url}")
    if ws.venv.exists():
        return
    ws.venv.parent.mkdir(parents=True, exist_ok=True)
    try:
        must(
            sh(["uv", "venv", "-q", "--python", ws.project.python, str(ws.venv)], ws.root),
            "uv venv",
        )
        packages = [*PINS, *ws.project.requirements]
        install = ["uv", "pip", "install", "-q", "--python", ws.tool("python")]
        install += ["--exclude-newer", EXCLUDE_NEWER, *packages]
        must(sh(install, ws.root), "uv pip install")
    except RuntimeError:
        shutil.rmtree(ws.venv, ignore_errors=True)
        raise


def checkout(ws: Workspace, label: str, sha: str) -> Path:
    """A fresh detached worktree of `sha`."""
    path = ws.root / "trees" / f"{ws.project.name}-{label}"
    if path.exists():
        sh(["git", "worktree", "remove", "--force", str(path)], ws.clone)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(ws.clone, "worktree", "add", "-q", "--detach", str(path), sha)
    return path


# --------------------------------------------------------------------------- code structure
@dataclass(frozen=True)
class Function:
    def_line: int
    first: int  # first line, decorators included
    last: int
    docstring: Span | None


def docstring_span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Span | None:
    head = node.body[0]
    is_text = isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
    if is_text and isinstance(head.value.value, str):  # type: ignore[attr-defined]
        return head.lineno, head.end_lineno or head.lineno
    return None


def functions_of(text: str) -> dict[str, Function]:
    """`name` / `Class.name` -> where it lives. Top-level functions and direct methods only:
    what mutmut mutates and what the Golden selectors name."""
    found: dict[str, Function] = {}
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)

    def add(owner: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        key = f"{owner}.{node.name}" if owner else node.name
        found[key] = Function(
            node.lineno, first, node.end_lineno or node.lineno, docstring_span(node)
        )

    for node in ast.parse(text).body:
        if isinstance(node, kinds):
            add("", node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, kinds):
                    add(node.name, item)
    return found


def statement_spans(text: str) -> list[Span]:
    """Own lines of every statement: just the header of a compound one."""
    spans: list[Span] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.stmt):
            body = getattr(node, "body", None)
            end = body[0].lineno - 1 if isinstance(body, list) and body else node.end_lineno
            spans.append((node.lineno, max(node.lineno, end or node.lineno)))
    return spans


def statement_of(line: int, spans: list[Span]) -> Span | None:
    """The innermost statement whose own lines include `line`."""
    hits = [s for s in spans if s[0] <= line <= s[1]]
    return min(hits, key=lambda s: s[1] - s[0]) if hits else None


# --------------------------------------------------------------------------- diffs
Hunk = list[tuple[str, str]]  # (" " | "-" | "+", text)


@dataclass(frozen=True)
class Change:
    """One hunk of a `-U0` diff: `count` old lines from `start` became the `added` lines."""

    start: int
    count: int
    new_start: int = 0
    added: tuple[str, ...] = ()


def changes(diff: str) -> list[Change]:
    found: list[Change] = []
    for raw in diff.splitlines():
        if m := HUNK.match(raw):
            old_count = 1 if m[2] is None else int(m[2])
            found.append(Change(int(m[1]), old_count, int(m[3])))
        elif raw.startswith("+") and found and not raw.startswith("+++"):
            last = found[-1]
            found[-1] = Change(last.start, last.count, last.new_start, (*last.added, raw[1:]))
    return found


def is_code(line: str) -> bool:
    return bool(line.strip()) and not line.strip().startswith("#")


def parse_hunks(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    for raw in diff.splitlines():
        if raw.startswith("@@"):
            hunks.append([])
        elif hunks and raw[:1] in {" ", "-", "+"}:
            hunks[-1].append((raw[0], raw[1:]))
    return hunks


def place(hunk: Hunk, lines: list[str], lo: int, hi: int) -> tuple[int, list[int]] | None:
    """Find where a mutmut hunk applies in the file. mutmut numbers lines from the start of
    the function *snippet* (comments above the `def` belong to it), so match the text:
    returns the 0-based start index and the 1-based numbers of the removed lines."""
    old = [(op, text.strip()) for op, text in hunk if op != "+"]
    for start in range(max(0, lo - 13), hi):
        window = lines[start : start + len(old)]
        if len(window) == len(old) and all(
            w.strip() == t for w, (_, t) in zip(window, old, strict=True)
        ):
            return start, [start + i + 1 for i, (op, _) in enumerate(old) if op == "-"]
    return None


def apply_hunks(lines: list[str], placed: list[tuple[Hunk, int]]) -> list[str]:
    out = list(lines)
    for hunk, start in sorted(placed, key=lambda p: -p[1]):
        old_len = sum(1 for op, _ in hunk if op != "+")
        out[start : start + old_len] = [t for op, t in hunk if op != "-"]
    return out


def canon(lines: list[str]) -> list[str]:
    """Code without blank lines, comments or layout, to compare a mutant with the fix."""
    code = (re.sub(r"\s+", " ", ln.split("#")[0]).strip() for ln in lines)
    return [c for c in code if c]


def fix_size(diff: str, old_text: str, new_text: str, names: tuple[str, ...]) -> int:
    """Code lines the fix removes plus adds inside the buggy functions (no comments, blanks
    or docstrings): the size of the fix."""

    def code_lines(text: str) -> set[int]:
        found = functions_of(text)
        lines = text.splitlines()
        out: set[int] = set()
        for fn in (found[n] for n in names if n in found):
            doc = fn.docstring or (0, 0)
            out |= {
                n
                for n in range(fn.first, fn.last + 1)
                if is_code(lines[n - 1]) and not doc[0] <= n <= doc[1]
            }
        return out

    old_code, new_code = code_lines(old_text), code_lines(new_text)
    size = 0
    for c in changes(diff):
        size += len(old_code & set(range(c.start, c.start + c.count)))
        size += len(new_code & set(range(c.new_start, c.new_start + len(c.added))))
    return size


# --------------------------------------------------------------------------- faulty lines
@dataclass
class Fault:
    kind: str  # "modified": F deletes or rewrites lines; "omission": F only inserts code
    lines: list[int]
    statement_lines: list[int]
    functions: list[str]
    fix_size: int = 0


def faulty_lines(parent_text: str, diff: str, names: tuple[str, ...]) -> Fault:
    """The lines F changes inside the buggy functions, in P's numbering.

    Comment, blank and docstring edits do not count. When F only inserts code (adds a
    missing check) nothing in P is deleted, so the statements just before and after the
    insertion point stand in for the fault (kind "omission").
    """
    found, stmts = functions_of(parent_text), statement_spans(parent_text)
    text = parent_text.splitlines()
    modified: set[int] = set()
    anchors: set[int] = set()
    for change in changes(diff):
        for fn in (found[n] for n in names):
            doc = fn.docstring or (0, 0)
            old = range(change.start, change.start + change.count)
            removed = {
                n
                for n in old
                if fn.first <= n <= fn.last and is_code(text[n - 1]) and not doc[0] <= n <= doc[1]
            }
            at = change.start if not change.count else change.start - 1
            if removed:
                modified |= removed
            elif any(map(is_code, change.added)) and fn.first <= at <= fn.last:
                firsts = [s[0] for s in stmts if fn.first <= s[0] <= fn.last]
                before = max((s for s in firsts if s <= at), default=0)
                after = min((s for s in firsts if s > at), default=0)
                anchors |= {x for x in (before, after) if x}
    lines = sorted(modified or anchors)
    expanded = {
        n for line in lines if (s := statement_of(line, stmts)) for n in range(s[0], s[1] + 1)
    }
    return Fault(
        "modified" if modified else "omission", lines, sorted(expanded | set(lines)), list(names)
    )


# --------------------------------------------------------------------------- step 1: verify
def changed_tests(ws: Workspace, bug: Bug) -> list[str]:
    names = git(ws.clone, "diff", "--name-only", bug.parent, bug.fix).split()
    return [n for n in names if n.endswith(".py") and n.startswith(ws.project.test_paths)]


def pytest_failures(ws: Workspace, tree: Path, targets: list[str]) -> tuple[int, set[str]]:
    cmd = [ws.tool("python"), "-m", "pytest", *ws.pytest_args(), f"--timeout={VERIFY_TIMEOUT}"]
    proc = sh([*cmd, "-rfE", "--tb=no", *targets], tree, ws.env())
    return proc.returncode, set(TEST_FAIL.findall(proc.stdout))


def verify(ws: Workspace, bug: Bug) -> dict[str, Any]:
    """The regression test must fail on P (with F's test files) and pass on F."""
    tests = changed_tests(ws, bug)
    at_parent = checkout(ws, "verify-p", bug.parent)
    git(at_parent, "checkout", "-q", bug.fix, "--", *tests)
    _, failing = pytest_failures(ws, at_parent, tests)
    code, failing_fix = pytest_failures(ws, checkout(ws, "verify-f", bug.fix), tests)
    return {
        "changed_test_files": tests,
        "failing_at_parent": sorted(failing),
        "failing_at_fix": sorted(failing_fix),
        "regression_fails_at_parent": names_failing(bug.regression_tests, failing),
        "regression_passes_at_fix": code == 0,
    }


def names_failing(expected: tuple[str, ...], failing: set[str]) -> bool:
    """Every listed test (or parametrized family `test[...]`) fails."""
    return bool(expected) and all(
        any(f == e or f.startswith(e + "[") for f in failing) for e in expected
    )


# --------------------------------------------------------------------------- step 2a: coverage
def coverage_of(ws: Workspace, bug: Bug, fault: Fault, tree: Path) -> dict[str, Any]:
    """Were the faulty lines executed by the suite that existed at P? (branch coverage on)"""
    report = tree / "coverage.rf.json"
    env = {**ws.env(), "COVERAGE_FILE": str(tree / ".coverage.rf")}
    run = [ws.tool("coverage"), "run", "--branch", f"--include={bug.file}", "-m", "pytest"]
    run += [*ws.pytest_args(), *(ws.project.run_paths or ws.project.test_paths)]
    proc = sh(run, tree, env)
    if proc.returncode != 0:
        raise RuntimeError(f"P's own suite does not pass here: {proc.stdout[-1000:]}")
    must(sh([ws.tool("coverage"), "json", "-q", "-o", str(report)], tree, env), "coverage json")
    entry = json.loads(report.read_text(encoding="utf-8"))["files"][bug.file]
    return coverage_verdict(fault, (tree / bug.file).read_text(encoding="utf-8"), entry)


def coverage_verdict(fault: Fault, text: str, entry: dict[str, Any]) -> dict[str, Any]:
    executed, missing = set(entry["executed_lines"]), set(entry["missing_lines"])
    stmts = statement_spans(text)
    firsts = {s[0] for line in fault.lines if (s := statement_of(line, stmts))}
    firsts &= executed | missing  # drop comments, blank lines and docstrings
    covered = sorted(firsts & executed)
    partial = sorted({a for a, _ in entry.get("missing_branches", []) if a in firsts})
    percent = round(100 * len(covered) / len(firsts), 1) if firsts else None
    return {
        "executable_faulty_lines": sorted(firsts),
        "executed": covered,
        "not_executed": sorted(firsts - executed),
        "partial_branch_lines": partial,
        "percent": percent,
        "all_executed": bool(firsts) and firsts <= executed,
        "diff_coverage_gate": "pass"
        if percent is not None and percent >= GATE_DIFF_MIN
        else "block",
    }


# --------------------------------------------------------------------------- step 2b/3: mutation
@dataclass
class Survivor:
    name: str
    status: str  # "survived" | "no tests"
    function: str = ""
    lines: list[int] = field(default_factory=list)
    on_fault_line: bool = False
    on_fault_statement: bool = False
    in_docstring: bool = False
    fix_equivalent: bool = False
    similarity: float = 0.0
    diff: str = ""


@dataclass
class MutationRun:
    selectors: list[str]
    killed: int = 0
    timeout: int = 0
    survived: int = 0
    no_tests: int = 0
    mutants: int = 0
    score: float | None = None
    gate: str = "error"
    gate_exit: int = -1
    survived_docstring: int = 0
    score_without_docstrings: float | None = None
    gate_without_docstrings: str = "error"
    seconds: float = 0.0
    survivors: list[Survivor] = field(default_factory=list)


def write_mutmut_config(ws: Workspace, bug: Bug, tree: Path) -> None:
    """Project-local mutmut configuration, written into the scratch tree only."""
    lines = ["", "[mutmut]", f"source_paths = {' '.join(ws.project.source_paths)}"]
    lines += [f"only_mutate = {bug.file}", "pytest_add_cli_args ="]
    lines += [f"    {arg}" for arg in (*MUTMUT_ARGS, *ws.extra_args())]
    if ws.project.also_copy:
        lines += ["also_copy ="] + [f"    {p}" for p in ws.project.also_copy]
    lines += list(ws.project.mutmut_config)
    cfg = tree / "setup.cfg"
    existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    cfg.write_text(existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


def selectors(bug: Bug, parent_text: str, diff: str) -> list[str]:
    """Golden's own mutmut selectors for the functions the fix changes (P's numbering)."""
    ranges = [(c.start, c.start + max(c.count, 1) - 1) for c in changes(diff)]
    found = presets.get("python").mutation_targets(bug.file, ranges, parent_text)
    module = presets.PythonPreset.module(bug.file)
    expected = sorted(
        f"{module}.xǁ{name.split('.')[0]}ǁ{name.split('.')[1]}__mutmut_*"
        if "." in name
        else f"{module}.x_{name}__mutmut_*"
        for name in bug.functions
    )
    if sorted(found) != expected:
        raise RuntimeError(f"selectors {found} do not match the manifest's functions {expected}")
    return found


def mutmut(ws: Workspace, tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return sh([ws.tool("mutmut"), *args], tree, ws.env())


def gate_verdict(tree: Path, env: dict[str, str]) -> tuple[str, int, float | None]:
    """The real `golden mutation` gate on the exported mutmut stats (platform defaults)."""
    cmd = [sys.executable, "-m", "golden", "mutation", "mutants/mutmut-cicd-stats.json"]
    proc = sh([*cmd, "--format", "mutmut"], tree, {**env, "PYTHONPATH": str(REPO / "src")})
    found = SCORE.search(proc.stdout + proc.stderr)
    verdict = {0: "pass", 1: "block"}.get(proc.returncode, "error")
    return verdict, proc.returncode, float(found[1]) if found else None


def count_mutants(run: MutationRun, stats: dict[str, int]) -> None:
    # `suspicious` and `segfault` behave like timeouts, as in golden.mutation._mutmut.
    run.killed, run.survived, run.no_tests = stats["killed"], stats["survived"], stats["no_tests"]
    run.timeout = stats["timeout"] + stats["suspicious"] + stats["segfault"]
    run.mutants = run.killed + run.timeout + run.survived + run.no_tests


def run_mutation(ws: Workspace, bug: Bug, tree: Path, targets: list[str]) -> MutationRun:
    """mutmut on the selected functions, scored by the Golden gate."""
    started = time.monotonic()
    run = MutationRun(targets)
    write_mutmut_config(ws, bug, tree)
    proc = mutmut(ws, tree, "run", *targets)
    (tree / "mutmut-run.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    must(mutmut(ws, tree, "export-cicd-stats"), "mutmut export-cicd-stats")
    count_mutants(run, json.loads((tree / "mutants" / "mutmut-cicd-stats.json").read_text()))
    if run.mutants == 0:
        raise RuntimeError(f"mutmut gave no verdicts: {proc.stdout[-600:]}")
    run.gate, run.gate_exit, run.score = gate_verdict(tree, ws.env())
    run.seconds = round(time.monotonic() - started, 1)
    return run


def list_survivors(ws: Workspace, tree: Path, patterns: list[str]) -> list[Survivor]:
    out = must(mutmut(ws, tree, "results", "--all", "true"), "mutmut results")
    found = re.findall(r"^\s+(\S+): (survived|no tests)$", out, re.MULTILINE)
    return [Survivor(n, s) for n, s in found if any(fnmatch.fnmatch(n, p) for p in patterns)]


def locate(s: Survivor, text: str, fault: Fault | None) -> None:
    """Which source lines the survivor changes, and whether they are faulty or a docstring."""
    m = MUTANT.search(s.name)
    funcs = functions_of(text)
    key = (f"{m['cls']}.{m['fn']}" if m["cls"] else m["fn"]) if m else ""
    if key not in funcs:
        return
    fn, lines = funcs[key], text.splitlines()
    s.function = key
    for hunk in parse_hunks(s.diff):
        found = place(hunk, lines, fn.first, fn.last)
        s.lines += found[1] if found else []
    changed = set(s.lines)
    if fn.docstring and changed:
        s.in_docstring = all(fn.docstring[0] <= n <= fn.docstring[1] for n in changed)
    if fault:
        s.on_fault_line = bool(changed & set(fault.lines))
        s.on_fault_statement = bool(changed & set(fault.statement_lines))


def equals_fix(s: Survivor, parent_text: str, fixed: list[str]) -> bool:
    """Does applying this one mutation to P's function give exactly F's function?"""
    funcs, lines = functions_of(parent_text), parent_text.splitlines()
    if s.function not in funcs:
        return False
    fn = funcs[s.function]
    placed = [(h, f[0]) for h in parse_hunks(s.diff) if (f := place(h, lines, fn.first, fn.last))]
    if not placed:
        return False
    mutated = apply_hunks(lines, placed)
    grown = len(mutated) - len(lines)
    return canon(mutated[fn.first - 1 : fn.last + grown]) == canon(fixed)


def added_text(diff: str) -> str:
    """The code a diff adds, on one line."""
    return " ".join(t.strip() for h in parse_hunks(diff) for op, t in h if op == "+")


def similarity(s: Survivor, fix_added: str) -> float:
    added = added_text(s.diff)
    return round(difflib.SequenceMatcher(None, added, fix_added).ratio(), 3) if added else 0.0


def describe_survivors(
    ws: Workspace, tree: Path, run: MutationRun, text: str, fault: Fault | None
) -> None:
    """Fetch each survivor's diff with `mutmut show`, then locate it in the source."""
    run.survivors = list_survivors(ws, tree, run.selectors)
    listed, counted = len(run.survivors), run.survived + run.no_tests
    if listed != counted:  # one mutmut state feeds both: a mismatch is unparsable output
        raise RuntimeError(f"mutmut results lists {listed} survivors, its stats count {counted}")

    def show(name: str) -> str:
        shown = must(mutmut(ws, tree, "show", name), f"mutmut show {name}")
        diff = "\n".join(shown.splitlines()[1:]).strip("\n")
        if not parse_hunks(diff):
            raise RuntimeError(f"mutmut show {name} printed no diff: {shown[-300:]}")
        return diff

    with ThreadPoolExecutor(max_workers=4) as pool:  # read-only, one process per call
        diffs = list(pool.map(show, [s.name for s in run.survivors]))
    for s, diff in zip(run.survivors, diffs, strict=True):
        s.diff = diff
        locate(s, text, fault)
    doc = [s for s in run.survivors if s.in_docstring]
    run.survived_docstring = sum(s.status == "survived" for s in doc)
    no_tests_doc = sum(s.status == "no tests" for s in doc)
    tally = mutation.Tally(
        run.killed, run.timeout, run.survived - run.survived_docstring, run.no_tests - no_tests_doc
    )
    run.score_without_docstrings = tally.score
    ok = tally.score is None or (tally.score >= GATE_MIN_SCORE and not tally.unreliable)
    run.gate_without_docstrings = "pass" if ok else "block"


def rank_survivors(
    run: MutationRun, parent_text: str, fixed: dict[str, list[str]], diff: str
) -> None:
    fix_added = added_text(diff)
    for s in run.survivors:
        s.fix_equivalent = equals_fix(s, parent_text, fixed.get(s.function, []))
        s.similarity = similarity(s, fix_added)


def best_survivor(run: MutationRun) -> Survivor | None:
    """The survivor on a faulty line that looks most like the fix (equal to it, if any)."""
    pool = [s for s in run.survivors if s.on_fault_line and not s.in_docstring]
    pool = pool or [s for s in run.survivors if s.on_fault_statement and not s.in_docstring]
    return max(pool, key=lambda s: (s.fix_equivalent, s.similarity), default=None)


# --------------------------------------------------------------------------- orchestration
def evaluate_bug(ws: Workspace, bug: Bug) -> dict[str, Any]:
    """Steps 1-3 for one bug; raises RuntimeError with a reason when a tool step fails."""
    timings: dict[str, float] = {}
    clock = time.monotonic()

    def lap(name: str) -> None:
        nonlocal clock
        timings[name], clock = round(time.monotonic() - clock, 1), time.monotonic()

    result: dict[str, Any] = {"id": bug.id, "project": bug.project, "file": bug.file}
    result["verify"] = verify(ws, bug)
    lap("verify")
    parent, fix = checkout(ws, "p", bug.parent), checkout(ws, "f", bug.fix)
    parent_text = (parent / bug.file).read_text(encoding="utf-8")
    fix_text = (fix / bug.file).read_text(encoding="utf-8")
    diff = git(ws.clone, "diff", "-U0", bug.parent, bug.fix, "--", bug.file)
    fault = faulty_lines(parent_text, diff, bug.functions)
    fault.fix_size = fix_size(diff, parent_text, fix_text, bug.functions)
    result["fault"] = asdict(fault)
    result["coverage"] = coverage_of(ws, bug, fault, parent)
    lap("coverage")
    targets = selectors(bug, parent_text, diff)
    result["selectors"] = targets
    p_run = run_mutation(ws, bug, parent, targets)
    describe_survivors(ws, parent, p_run, parent_text, fault)
    funcs, fixed_funcs = functions_of(fix_text), {}
    fix_lines = fix_text.splitlines()
    fixed_funcs = {
        n: fix_lines[f.first - 1 : f.last] for n, f in funcs.items() if n in bug.functions
    }
    rank_survivors(p_run, parent_text, fixed_funcs, diff)
    lap("mutation_parent")
    f_run = run_mutation(ws, bug, fix, targets)
    describe_survivors(ws, fix, f_run, fix_text, None)
    lap("mutation_fix")
    result["gates"] = gate_decisions(result["coverage"], p_run)
    best = best_survivor(p_run)
    result.update(mutation_parent=asdict(p_run), mutation_fix=asdict(f_run), seconds=timings)
    result["best_survivor"] = asdict(best) if best else None
    return result


def gate_decisions(cov: dict[str, Any], p_run: MutationRun) -> dict[str, str]:
    """What each gate would have said about the buggy code with the tests of that day."""
    return {
        # Needs the change that introduced the bug, which this benchmark does not reconstruct.
        "test_presence": "not evaluated",
        "diff_coverage": cov["diff_coverage_gate"],
        "mutation": p_run.gate,
        # A pipeline with coverage gates only (diff coverage, no mutation): flags or misses.
        "coverage_only_pipeline": "flags" if cov["diff_coverage_gate"] == "block" else "misses",
        "full_pipeline": "flags"
        if "block" in (cov["diff_coverage_gate"], p_run.gate)
        else "misses",
    }


# --------------------------------------------------------------------------- reporting
def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def headline(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The headline metrics, computed from the per-bug records only."""
    ran = [r for r in results if "skipped" not in r]
    ok = [r for r in ran if "error" not in r]
    p, f = [r["mutation_parent"] for r in ok], [r["mutation_fix"] for r in ok]
    blind = [r["coverage"]["all_executed"] for r in ok]
    blocks = [m["gate"] == "block" for m in p]
    coupled = [any(s["on_fault_line"] for s in m["survivors"]) for m in p]
    pairs = list(zip(p, f, strict=True))
    return {
        "bugs_evaluated": len(ok),
        "bugs_errored": len(ran) - len(ok),
        "bugs_skipped": len(results) - len(ran),
        "faulty_lines_all_executed_at_P": sum(blind),
        "diff_coverage_gate_passes_at_P": sum(
            r["coverage"]["diff_coverage_gate"] == "pass" for r in ok
        ),
        "mutation_gate_blocks_at_P": sum(blocks),
        "mutation_gate_blocks_at_P_without_docstring_mutants": sum(
            m["gate_without_docstrings"] == "block" for m in p
        ),
        "survivor_on_faulty_line_at_P": sum(coupled),
        "survivor_on_faulty_line_at_P_excluding_docstrings": sum(
            any(s["on_fault_line"] and not s["in_docstring"] for s in m["survivors"]) for m in p
        ),
        "fix_is_a_surviving_mutant_at_P": sum(
            any(s["fix_equivalent"] for s in m["survivors"]) for m in p
        ),
        "score_raised_at_F": sum(fm["score"] > pm["score"] for pm, fm in pairs),
        "score_equal_at_F": sum(fm["score"] == pm["score"] for pm, fm in pairs),
        "score_lower_at_F": sum(fm["score"] < pm["score"] for pm, fm in pairs),
        "gate_blocks_at_F": sum(m["gate"] == "block" for m in f),
        "coverage_blind_and_gate_blocks": sum(a and b for a, b in zip(blind, blocks, strict=True)),
        "coverage_blind_and_survivor_on_fault": sum(
            a and b for a, b in zip(blind, coupled, strict=True)
        ),
        "coverage_blind_and_gate_passes": sum(
            a and not b for a, b in zip(blind, blocks, strict=True)
        ),
        "coverage_only_pipeline_flags": sum(
            r["gates"]["coverage_only_pipeline"] == "flags" for r in ok
        ),
        "full_pipeline_flags": sum(r["gates"]["full_pipeline"] == "flags" for r in ok),
        "median_score_at_P": median([m["score"] for m in p if m["score"] is not None]),
        "median_score_at_F": median([m["score"] for m in f if m["score"] is not None]),
    }


def cell(score: float | None, gate: str) -> str:
    return "n/a" if score is None else f"{score:g}% {gate.upper()}"


def row(r: dict[str, Any]) -> str:
    if "skipped" in r:
        return f"| {r['id']} | | | | {r['skipped'][:60]}... | | | | | |"
    if "error" in r:
        return f"| {r['id']} | | | | error: {r['error'][:80]} | | | | | |"
    p, f, c = r["mutation_parent"], r["mutation_fix"], r["coverage"]
    survivors = p["survivors"]
    cov = f"{'yes' if c['all_executed'] else 'NO'} ({c['percent']}%)"
    coupled = any(s["on_fault_line"] for s in survivors)
    equal = any(s["fix_equivalent"] for s in survivors)
    g = r["gates"]
    return (
        f"| {r['id']} | {', '.join(r['fault']['functions'])} | {r['fault']['kind']} | {cov} | "
        f"{cell(p['score'], p['gate'])} ({p['survived'] + p['no_tests']} surv.) | "
        f"{'yes' if coupled else 'no'} | {'yes' if equal else 'no'} | "
        f"{cell(f['score'], f['gate'])} | {g['coverage_only_pipeline']} | {g['full_pipeline']} |"
    )


def table(results: list[dict[str, Any]]) -> str:
    head = (
        "| Bug | Function(s) | Fault | Faulty lines executed at P | Score at P / gate | "
        "Survivor on faulty line | Fix is a survivor | Score at F / gate | "
        "Coverage-only pipeline | Full pipeline |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    return head + "\n".join(row(r) for r in results) + "\n"


HEADLINE_ROWS = (
    (
        "coverage-only pipeline (diff coverage) flags the faulty code",
        "coverage_only_pipeline_flags",
    ),
    ("full pipeline (diff coverage + mutation) flags it", "full_pipeline_flags"),
    ("faulty lines all executed at P (coverage blind spot)", "faulty_lines_all_executed_at_P"),
    ("diff-coverage gate passes the faulty lines", "diff_coverage_gate_passes_at_P"),
    ("mutation gate blocks at P", "mutation_gate_blocks_at_P"),
    ("... without docstring mutants", "mutation_gate_blocks_at_P_without_docstring_mutants"),
    ("a surviving mutant on a faulty line at P", "survivor_on_faulty_line_at_P"),
    ("... excluding docstring mutants", "survivor_on_faulty_line_at_P_excluding_docstrings"),
    ("the fix itself is a surviving mutant at P", "fix_is_a_surviving_mutant_at_P"),
    ("score higher at F than at P", "score_raised_at_F"),
    ("score equal at F and P", "score_equal_at_F"),
    ("score lower at F than at P", "score_lower_at_F"),
    ("mutation gate blocks at F", "gate_blocks_at_F"),
    ("coverage blind AND mutation gate blocks at P", "coverage_blind_and_gate_blocks"),
    ("coverage blind AND survivor on a faulty line", "coverage_blind_and_survivor_on_fault"),
    ("coverage blind AND mutation gate passes at P", "coverage_blind_and_gate_passes"),
)


def render_headline(h: dict[str, Any]) -> str:
    n = h["bugs_evaluated"]
    lines = [f"- bugs evaluated: {n} (tool errors: {h['bugs_errored']})"]
    lines += [f"- {label}: {pct(h[key], n)}" for label, key in HEADLINE_ROWS]
    lines.append(f"- median score: {h['median_score_at_P']} at P, {h['median_score_at_F']} at F")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--manifest", type=Path, default=HERE / "manifest.toml")
    p.add_argument("--only", action="append", default=[], metavar="ID", help="bug id (repeatable)")
    p.add_argument("--workdir", type=Path, help="scratch directory (default: a temp dir)")
    p.add_argument("--out", type=Path, help="where results.json and results.md go")
    p.add_argument("--keep", action="store_true", help="keep the scratch directory")
    p.add_argument(
        "--verify-only",
        action="store_true",
        help="only check regression tests fail on P, pass on F",
    )
    return p.parse_args(argv)


def run_all(args: argparse.Namespace) -> list[dict[str, Any]]:
    projects, bugs = load_manifest(args.manifest)
    if args.only:
        missing = set(args.only) - {b.id for b in bugs}
        if missing:
            raise SystemExit(f"unknown bug ids: {sorted(missing)}")
        bugs = [b for b in bugs if b.id in args.only]
    root = args.workdir or Path(tempfile.mkdtemp(prefix="realfaults-"))
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for bug in bugs:
        ws = Workspace(root, projects[bug.project])
        print(f"== {bug.id}", flush=True)
        if bug.skip:
            results.append({"id": bug.id, "project": bug.project, "skipped": bug.skip})
            continue
        try:
            prepare(ws)
            if args.verify_only:
                print(json.dumps(verify(ws, bug), indent=2))
                continue
            results.append(evaluate_bug(ws, bug))
        except (RuntimeError, KeyError, SyntaxError, subprocess.TimeoutExpired) as exc:
            print(f"   error: {exc}", flush=True)
            results.append({"id": bug.id, "project": bug.project, "error": str(exc)})
    if not args.workdir and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return results


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    results = run_all(args)
    summary = headline(results) if any("mutation_parent" in r for r in results) else {}
    # A partial run (--only) must not overwrite the committed results of the whole manifest.
    out = args.out or (Path(tempfile.gettempdir()) if args.only else HERE)
    document = {
        "headline": summary,
        "bugs": results,
        "runtime_seconds": round(time.monotonic() - started),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    markdown = table(results) + ("\n" + render_headline(summary) if summary else "")
    (out / "results.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0 if all("error" not in r for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
