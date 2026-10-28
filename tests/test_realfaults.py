"""The real-fault benchmark harness, evals/realfaults/run.py (evals.realfaults.run).

The harness itself needs the network, `uv` and hours of mutmut; none of that runs here.
Its logic is checked with small hand-written sources and diffs, its process plumbing against
scripted tools and tiny local git repositories (git itself runs for real, never against the
network), and the published numbers against the committed results.
"""

import ast
import copy
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import types
from pathlib import Path

import pytest

from golden import mutation

ROOT = Path(__file__).resolve().parent.parent
RF_DIR = ROOT / "evals" / "realfaults"
_spec = importlib.util.spec_from_file_location("evals_realfaults_run", RF_DIR / "run.py")
rf = importlib.util.module_from_spec(_spec)
sys.modules["evals_realfaults_run"] = rf
_spec.loader.exec_module(rf)

RESULTS = json.loads((RF_DIR / "results.json").read_text(encoding="utf-8"))
README = (RF_DIR / "README.md").read_text(encoding="utf-8")
RAW_MANIFEST = tomllib.loads((RF_DIR / "manifest.toml").read_text(encoding="utf-8"))
SHA = re.compile(r"[0-9a-f]{40}")


@pytest.fixture(autouse=True)
def _original_cli_in_subprocesses(monkeypatch):
    """`golden mutation` runs as a subprocess; under mutmut it must not inherit the active
    mutant (the same precaution as tests/test_drill.py)."""
    monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)


# ----------------------------------------------------------------------------- sample data
STRUCT = '''\
import os

CONST = 1


def top(a, b):
    """Doc one.

    More doc.
    """
    # comment
    if a > b:
        return a
    return b


class Box:
    x = 1

    @staticmethod
    def make(n):
        total = 0
        for i in range(n):
            total += i
        return total

    async def later(self):
        return 1

    def inner(self):
        def nested():
            return 2
        return nested
'''

CHECK = '''\
import math


def check(v, lo, hi):
    """Clamp v into [lo, hi]."""
    # bounds first
    if v < lo:
        return lo
    if v > hi:
        return hi - 1
    return v


def other():
    return 0
'''
# `check` spans lines 4-11 (docstring on 5, comment on 6); the bug is line 10.
FIXED_CHECK = CHECK.replace("hi - 1", "hi")

MULTI = """\
def build(a, b):
    out = make(
        a,
        b,
    )
    return out
"""

DECORATED = """\
@cache
def cached(x):
    return x
"""

SEL_SRC = """\
class Stats:
    def calc(self, x):
        return x + 1

    def other(self):
        return 0


def helper(y):
    return y
"""

TEST_P = """\
from pkg.mod import check


def test_low():
    assert check(0, 1, 5) == 1
"""
TEST_F = (
    TEST_P
    + """

def test_high():
    assert check(9, 1, 5) == 5
"""
)


def udiff(old: str, new: str) -> str:
    """What `git diff -U0` prints for two versions of a file."""
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), "a/m.py", "b/m.py", n=0, lineterm=""
    )
    return "\n".join(lines) + "\n"


def done(cmd=(), code=0, out="", err=""):
    return subprocess.CompletedProcess(list(cmd), code, out, err)


def project(**over):
    fields = {
        "name": "proj",
        "url": "https://example.invalid/proj",
        "python": "3.12",
        "source_paths": ("pkg/",),
        "test_paths": ("tests",),
    }
    return rf.Project(**{**fields, **over})


def bug(**over):
    fields = {
        "id": "proj-clamp-high",
        "project": "proj",
        "fix": "f" * 40,
        "parent": "a" * 40,
        "file": "pkg/mod.py",
        "functions": ("check",),
        "regression_tests": ("tests/test_mod.py::test_high",),
    }
    return rf.Bug(**{**fields, **over})


# ----------------------------------------------------------------------------- manifest
MANIFEST = """\
[[project]]
name = "alpha"
url = "https://example.invalid/alpha"
python = "3.12"
source_paths = ["alpha/"]
test_paths = ["tests"]
requirements = ["wcwidth"]
pytest_extra = ["--doctest-modules"]
snapshot = "abc123"

[[project]]
name = "beta"
url = "https://example.invalid/beta"
python = "3.13"
source_paths = ["src/", "lib/"]
test_paths = ["test"]
deselect = ["test/test_x.py::test_version"]
also_copy = ["README.md"]
run_paths = ["test", "src"]
mutmut_config = ["process_isolation = forkserver"]

[[bug]]
id = "alpha-one"
project = "alpha"
fix = "1111111111111111111111111111111111111111"
parent = "2222222222222222222222222222222222222222"
file = "alpha/core.py"
functions = ["f", "K.g"]
regression_tests = ["tests/test_core.py::test_f"]
issue = "https://example.invalid/issues/1"
summary = "an off-by-one"

[[bug]]
id = "beta-two"
project = "beta"
fix = "3333333333333333333333333333333333333333"
parent = "4444444444444444444444444444444444444444"
file = "src/b.py"
functions = ["h"]
regression_tests = ["test/test_b.py::test_h"]
skip = "too slow"
"""


def test_load_manifest_builds_typed_records_with_defaults(tmp_path):
    path = tmp_path / "manifest.toml"
    path.write_text(MANIFEST, encoding="utf-8")
    projects, bugs = rf.load_manifest(path)
    assert list(projects) == ["alpha", "beta"]
    assert projects["alpha"] == rf.Project(
        name="alpha",
        url="https://example.invalid/alpha",
        python="3.12",
        source_paths=("alpha/",),
        test_paths=("tests",),
        requirements=("wcwidth",),
        pytest_extra=("--doctest-modules",),
        snapshot="abc123",
    )
    beta = projects["beta"]
    assert beta.source_paths == ("src/", "lib/")
    assert beta.deselect == ("test/test_x.py::test_version",)
    assert beta.run_paths == ("test", "src")
    assert beta.mutmut_config == ("process_isolation = forkserver",)
    assert (beta.requirements, beta.pytest_extra, beta.snapshot) == ((), (), "")
    assert bugs == [
        rf.Bug(
            id="alpha-one",
            project="alpha",
            fix="1" * 40,
            parent="2" * 40,
            file="alpha/core.py",
            functions=("f", "K.g"),
            regression_tests=("tests/test_core.py::test_f",),
            issue="https://example.invalid/issues/1",
            summary="an off-by-one",
        ),
        rf.Bug(
            id="beta-two",
            project="beta",
            fix="3" * 40,
            parent="4" * 40,
            file="src/b.py",
            functions=("h",),
            regression_tests=("test/test_b.py::test_h",),
            skip="too slow",
        ),
    ]


def test_load_manifest_rejects_bugs_of_unknown_projects(tmp_path):
    path = tmp_path / "manifest.toml"
    path.write_text(MANIFEST.replace('project = "beta"', 'project = "gamma"'), encoding="utf-8")
    with pytest.raises(SystemExit, match=r"unknown projects \['gamma'\]"):
        rf.load_manifest(path)


@pytest.mark.parametrize(
    "field", ["id", "project", "fix", "parent", "file", "functions", "regression_tests"]
)
def test_load_manifest_fails_on_a_bug_missing_a_required_field(tmp_path, field):
    kept = [ln for ln in MANIFEST.splitlines() if not ln.startswith(f"{field} = ")]
    # Drop the field from both bugs; the project tables keep their own `project`-free keys.
    path = tmp_path / "manifest.toml"
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises((TypeError, KeyError, SystemExit)) as raised:
        rf.load_manifest(path)
    assert field in str(raised.value)


@pytest.mark.parametrize("field", ["name", "url", "python", "source_paths", "test_paths"])
def test_load_manifest_fails_on_a_project_missing_a_required_field(tmp_path, field):
    first, rest = MANIFEST.split('\n[[project]]\nname = "beta"', 1)
    kept = "\n".join(ln for ln in first.splitlines() if not ln.startswith(f"{field} = "))
    path = tmp_path / "manifest.toml"
    path.write_text(kept + '\n\n[[project]]\nname = "beta"' + rest, encoding="utf-8")
    with pytest.raises((TypeError, KeyError)) as raised:
        rf.load_manifest(path)
    assert field in str(raised.value)


def test_load_manifest_fails_on_an_unknown_field(tmp_path):
    path = tmp_path / "manifest.toml"
    path.write_text(MANIFEST.replace('summary = "an', 'sumary = "an'), encoding="utf-8")
    with pytest.raises(TypeError, match="sumary"):
        rf.load_manifest(path)


@pytest.mark.parametrize("text", ["", "[[project]]\n", "this is = not [toml"])
def test_load_manifest_fails_on_an_empty_or_broken_manifest(tmp_path, text):
    path = tmp_path / "manifest.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises((KeyError, tomllib.TOMLDecodeError)):
        rf.load_manifest(path)


def test_load_manifest_fails_on_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        rf.load_manifest(tmp_path / "nope.toml")


# ------------------------------------------------------------- the committed manifest and data
def test_the_committed_manifest_loads_and_every_bug_has_what_the_runner_needs():
    projects, bugs = rf.load_manifest(RF_DIR / "manifest.toml")
    assert sorted(projects) == ["boltons", "more-itertools", "semver", "tabulate", "toolz"]
    assert len(bugs) == 14
    assert len({b.id for b in bugs}) == len(bugs)
    for p in projects.values():
        assert p.url.startswith("https://github.com/"), p.name
        assert p.python, p.name
        assert p.source_paths, p.name
        assert p.test_paths, p.name
        assert SHA.fullmatch(p.snapshot), p.name
    for b in bugs:
        p = projects[b.project]
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", b.id), b.id
        assert b.id.startswith(b.project + "-"), b.id
        assert SHA.fullmatch(b.fix), b.id
        assert SHA.fullmatch(b.parent), b.id
        assert b.fix != b.parent, b.id
        assert b.file.endswith(".py"), b.id
        assert b.file.startswith(p.source_paths), b.id
        assert b.functions, b.id
        assert all(re.fullmatch(r"(\w+\.)?\w+", name) for name in b.functions), b.id
        assert b.regression_tests, b.id
        for test in b.regression_tests:
            path, _, name = test.partition("::")
            assert path.endswith(".py"), (b.id, test)
            assert path.startswith(p.test_paths), (b.id, test)
            assert name, (b.id, test)
        assert b.summary, b.id
        assert not b.issue or b.issue.startswith("https://github.com/"), b.id


def test_skipped_bugs_carry_a_reason_and_are_exactly_the_ones_the_results_skip():
    raw_bugs = RAW_MANIFEST["bug"]
    for entry in raw_bugs:
        if "skip" in entry:
            assert entry["skip"].strip(), f"{entry['id']}: an empty skip would run the bug"
    _, bugs = rf.load_manifest(RF_DIR / "manifest.toml")
    skipped = {b.id: b.skip for b in bugs if b.skip}
    assert sorted(skipped) == sorted(e["id"] for e in raw_bugs if "skip" in e)
    in_results = {r["id"]: r["skipped"] for r in RESULTS["bugs"] if "skipped" in r}
    assert in_results == skipped
    assert len(skipped) == 4


def test_every_candidate_is_recorded_and_each_discard_has_a_reason():
    discarded = RAW_MANIFEST["discarded"]
    names = {p["name"] for p in RAW_MANIFEST["project"]}
    for d in discarded:
        assert d["project"] in names
        assert SHA.fullmatch(d["commit"])
        assert d["subject"].strip(), d["commit"]
        assert d["reason"].strip(), d["commit"]
    assert len({d["commit"] for d in discarded}) == len(discarded)
    assert {d["commit"] for d in discarded}.isdisjoint(b["fix"] for b in RAW_MANIFEST["bug"])
    assert len(RAW_MANIFEST["bug"]) + len(discarded) == 77


def test_results_follow_the_manifest_bug_for_bug():
    _, bugs = rf.load_manifest(RF_DIR / "manifest.toml")
    assert [r["id"] for r in RESULTS["bugs"]] == [b.id for b in bugs]
    for b, r in zip(bugs, RESULTS["bugs"], strict=True):
        assert r["project"] == b.project
        assert "error" not in r
        if b.skip:
            continue
        assert r["file"] == b.file
        assert r["fault"]["functions"] == list(b.functions)
        parts = b.file.removesuffix(".py").split("/")
        module = ".".join(p for p in parts if p not in ("src", "__init__"))
        want = sorted(
            f"{module}.xǁ{n.split('.')[0]}ǁ{n.split('.')[1]}__mutmut_*"
            if "." in n
            else f"{module}.x_{n}__mutmut_*"
            for n in b.functions
        )
        assert sorted(r["selectors"]) == want
        assert r["verify"]["regression_fails_at_parent"] is True
        assert r["verify"]["regression_passes_at_fix"] is True
        assert r["verify"]["changed_test_files"]
        assert r["verify"]["failing_at_fix"] == []


def test_the_published_headline_is_what_the_per_bug_records_add_up_to():
    assert rf.headline(RESULTS["bugs"]) == RESULTS["headline"]


def test_the_headline_claims_of_the_benchmark_commit():
    h = RESULTS["headline"]
    assert h["bugs_evaluated"] == 10
    assert h["coverage_only_pipeline_flags"] == 3
    assert h["full_pipeline_flags"] == 4
    assert h["survivor_on_faulty_line_at_P"] == 6
    assert (h["bugs_skipped"], h["bugs_errored"]) == (4, 0)


def test_results_md_is_the_rendering_of_results_json():
    bugs, h = RESULTS["bugs"], RESULTS["headline"]
    expected = rf.table(bugs) + "\n" + rf.render_headline(h)
    assert (RF_DIR / "results.md").read_text(encoding="utf-8") == expected


def test_readme_totals_match_the_results_and_the_manifest():
    h = RESULTS["headline"]
    found = re.search(
        r"Bugs examined: (\d+) candidate fix commits from (\d+) projects\. "
        r"Kept after screening: (\d+)\. Run end to end: \*\*(\d+)\*\*\. "
        r"Not run \(time budget\): (\d+)\. Tool errors: (\d+)\.",
        README,
    )
    assert found
    examined, projects, kept, ran, skipped, errors = map(int, found.groups())
    assert examined == len(RAW_MANIFEST["bug"]) + len(RAW_MANIFEST["discarded"])
    assert projects == len(RAW_MANIFEST["project"])
    assert kept == len(RAW_MANIFEST["bug"])
    assert (ran, skipped, errors) == (h["bugs_evaluated"], h["bugs_skipped"], h["bugs_errored"])


def test_readme_table_states_the_headline_numbers():
    h = RESULTS["headline"]
    n = h["bugs_evaluated"]
    cells = {}
    for m in re.finditer(r"^\| ([^|]+?) \| ([^|]+?) \|$", README, re.MULTILINE):
        cells[m[1].replace("**", "")] = m[2]
    assert f"Metric (over the {n} bugs run)" in cells
    expected = {
        "Faulty lines already executed by the suite of the day (coverage blind spot)": (
            rf.pct(h["faulty_lines_all_executed_at_P"], n)
        ),
        "Diff-coverage gate (90%) would have passed the faulty lines": (
            rf.pct(h["diff_coverage_gate_passes_at_P"], n)
        ),
        "Mutation gate (80%) blocks on the buggy functions": rf.pct(
            h["mutation_gate_blocks_at_P"], n
        ),
        "... same, ignoring mutants that only change a docstring": rf.pct(
            h["mutation_gate_blocks_at_P_without_docstring_mutants"], n
        ),
        "A surviving mutant sits on a faulty line": rf.pct(h["survivor_on_faulty_line_at_P"], n),
        "... same, ignoring docstring mutants": rf.pct(
            h["survivor_on_faulty_line_at_P_excluding_docstrings"], n
        ),
        "The fix itself is a surviving mutant (the tool generated the exact fix and no test "
        "noticed)": rf.pct(h["fix_is_a_surviving_mutant_at_P"], n),
        "Coverage-only pipeline flags the faulty code": rf.pct(
            h["coverage_only_pipeline_flags"], n
        ),
        "Full pipeline (diff coverage + mutation) flags it": rf.pct(h["full_pipeline_flags"], n),
        "Mutation score higher at F than at P (regression test raised it)": (
            f"{rf.pct(h['score_raised_at_F'], n)} "
            f"(equal {h['score_equal_at_F']}, lower {h['score_lower_at_F']})"
        ),
        "Mutation gate still blocks at F, with the regression test": rf.pct(
            h["gate_blocks_at_F"], n
        ),
        "Median mutation score": (
            f"{h['median_score_at_P']}% at P, {h['median_score_at_F']}% at F"
        ),
        "Coverage blind AND mutation gate blocks": rf.pct(h["coverage_blind_and_gate_blocks"], n),
        "Coverage blind AND mutation gate passes (missed by both)": rf.pct(
            h["coverage_blind_and_gate_passes"], n
        ),
    }
    for label, value in expected.items():
        assert cells[label] == value, label


def test_readme_fault_kind_breakdown_matches_the_records():
    ran = [r for r in RESULTS["bugs"] if "fault" in r]
    for kind, text in [
        (
            "modified",
            "- modified: coverage blind 6/8, mutation gate blocks 3/8, survivor on faulty line 5/8",
        ),
        (
            "omission",
            "- omission: coverage blind 1/2, mutation gate blocks 1/2, survivor on faulty line 1/2",
        ),
    ]:
        of_kind = [r for r in ran if r["fault"]["kind"] == kind]
        blind = sum(r["coverage"]["all_executed"] for r in of_kind)
        blocks = sum(r["mutation_parent"]["gate"] == "block" for r in of_kind)
        coupled = sum(
            any(s["on_fault_line"] for s in r["mutation_parent"]["survivors"]) for r in of_kind
        )
        n = len(of_kind)
        assert text in README
        assert (
            f"- {kind}: coverage blind {blind}/{n}, mutation gate blocks {blocks}/{n}, "
            f"survivor on faulty line {coupled}/{n}"
        ) == text


def test_readme_per_bug_table_matches_the_records():
    rows = {}
    for line in README.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 10 and cells[0] in {r["id"] for r in RESULTS["bugs"]}:
            rows[cells[0]] = cells
    assert sorted(rows) == sorted(r["id"] for r in RESULTS["bugs"])
    for r in RESULTS["bugs"]:
        if "skipped" in r:
            assert rows[r["id"]] == [r["id"], "", "", "", "not run", "", "", "", "", ""]
            continue
        p, f, c, g = r["mutation_parent"], r["mutation_fix"], r["coverage"], r["gates"]
        assert rows[r["id"]] == [
            r["id"],
            r["fault"]["kind"],
            f"{'yes' if c['all_executed'] else 'no'} ({c['percent']}%)",
            c["diff_coverage_gate"],
            f"{p['score']}% ({p['survived'] + p['no_tests']})",
            p["gate"],
            g["coverage_only_pipeline"],
            g["full_pipeline"],
            "yes" if any(s["on_fault_line"] for s in p["survivors"]) else "no",
            f"{f['score']}% ({f['gate']})",
        ]


def ran_results():
    return [r for r in RESULTS["bugs"] if "mutation_parent" in r]


@pytest.mark.parametrize("record", ran_results(), ids=lambda r: r["id"])
def test_every_run_record_is_internally_consistent(record):
    cov = record["coverage"]
    firsts, executed = cov["executable_faulty_lines"], cov["executed"]
    assert set(executed) | set(cov["not_executed"]) == set(firsts)
    assert set(executed).isdisjoint(cov["not_executed"])
    assert cov["percent"] == round(100 * len(executed) / len(firsts), 1)
    assert cov["all_executed"] == (not cov["not_executed"])
    assert cov["diff_coverage_gate"] == ("pass" if cov["percent"] >= rf.GATE_DIFF_MIN else "block")
    for key in ("mutation_parent", "mutation_fix"):
        m = record[key]
        assert m["selectors"] == record["selectors"]
        assert m["mutants"] == m["killed"] + m["timeout"] + m["survived"] + m["no_tests"]
        assert m["gate"] == ("pass" if m["score"] >= rf.GATE_MIN_SCORE else "block")
        assert m["gate_exit"] == {"pass": 0, "block": 1}[m["gate"]]
        # the two invariants describe_survivors enforces: they hold for every committed run, so
        # the guards cannot change what the committed results say
        assert len(m["survivors"]) == m["survived"] + m["no_tests"]
        assert all(rf.parse_hunks(s["diff"]) for s in m["survivors"])
        tally = mutation.Tally(m["killed"], m["timeout"], m["survived"], m["no_tests"])
        assert tally.score == m["score"]
        doc = [s for s in m["survivors"] if s["in_docstring"]]
        in_doc = sum(s["status"] == "survived" for s in doc)
        assert m["survived_docstring"] == in_doc
        rescored = mutation.Tally(
            m["killed"],
            m["timeout"],
            m["survived"] - in_doc,
            m["no_tests"] - sum(s["status"] == "no tests" for s in doc),
        )
        assert rescored.score == m["score_without_docstrings"]
    parent = record["mutation_parent"]
    run = rf.MutationRun(**{**parent, "survivors": [rf.Survivor(**s) for s in parent["survivors"]]})
    assert rf.gate_decisions(cov, run) == record["gates"]
    assert record["gates"]["test_presence"] == "not evaluated"


# ----------------------------------------------------------------------------- processes
def test_clean_env_drops_the_ci_context_and_pins_the_hash_seed(monkeypatch):
    values = {
        "GITHUB_TOKEN": "t",
        "GITHUB_ACTIONS": "true",
        "VIRTUAL_ENV": "/v",
        "VIRTUAL_ENV_PROMPT": "p",
        "CI": "true",
        "PYTHONHASHSEED": "42",
        "KEEP_ME": "1",
        "PATH": "/usr/bin",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    env = rf.clean_env()
    assert not [k for k in env if k.startswith(("GITHUB_", "VIRTUAL_ENV"))]
    assert "CI" not in env
    assert env["PYTHONHASHSEED"] == "0"
    assert (env["KEEP_ME"], env["PATH"]) == ("1", "/usr/bin")
    assert rf.clean_env(Path("/v"))["PATH"] == f"/v/bin{os.pathsep}/usr/bin"


def test_clean_env_without_a_path_fails_rather_than_guessing(monkeypatch):
    monkeypatch.delenv("PATH")
    assert "PATH" not in rf.clean_env()
    with pytest.raises(KeyError):
        rf.clean_env(Path("/v"))


def test_sh_passes_the_documented_arguments_to_subprocess(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return done(cmd, 7, "o", "e")

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc = rf.sh(["tool", "--flag"], tmp_path, {"K": "V"})
    assert seen["cmd"] == ["tool", "--flag"]
    assert seen["kwargs"] == {
        "cwd": tmp_path,
        "env": {"K": "V"},
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": 4 * 3600,
        "check": False,
    }
    assert (proc.returncode, proc.stdout, proc.stderr) == (7, "o", "e")
    rf.sh(["tool"], tmp_path)
    assert seen["kwargs"]["env"] == rf.clean_env()


CHILD = (
    "import json, os, sys\n"
    "json.dump({k: os.environ.get(k) for k in ('GITHUB_TOKEN', 'CI', 'PYTHONHASHSEED')}"
    " | {'cwd': os.getcwd()}, sys.stdout)\n"
    # Flush the text layer first, or the raw byte overtakes it when stdout is buffered.
    "sys.stdout.flush()\n"
    "sys.stdout.buffer.write(b'\\xff')\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write('warn')\n"
    "sys.exit(3)\n"
)


def test_sh_runs_a_real_command_and_never_raises_on_a_failing_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("CI", "true")
    proc = rf.sh([sys.executable, "-c", CHILD], tmp_path)
    assert proc.returncode == 3
    assert proc.stderr == "warn"
    assert proc.stdout.endswith("\ufffd")  # invalid bytes are replaced, not an error
    assert json.loads(proc.stdout.removesuffix("\ufffd")) == {
        "GITHUB_TOKEN": None,
        "CI": None,
        "PYTHONHASHSEED": "0",
        "cwd": str(tmp_path.resolve()),
    }


def test_must_returns_stdout_on_success():
    assert rf.must(done(code=0, out="fine\n", err="noise"), "thing") == "fine\n"


def test_must_raises_with_the_exit_code_and_the_tail_of_the_output():
    proc = done(code=2, out="x" * 2000, err="END")
    with pytest.raises(RuntimeError) as raised:
        rf.must(proc, "the thing")
    assert str(raised.value) == "the thing failed (exit 2): " + "x" * 1497 + "END"


def test_git_returns_stdout_and_names_the_failing_subcommand(repo):
    head = repo.commit({"a.txt": "1\n"})
    assert rf.git(repo.root, "rev-parse", "HEAD") == head + "\n"
    with pytest.raises(RuntimeError, match=r"^git rev-parse --verify failed \(exit 128\)"):
        rf.git(repo.root, "rev-parse", "--verify", "no-such-ref", "--quiet-not")


# ----------------------------------------------------------------------------- workspace
def test_workspace_paths_and_pytest_arguments(tmp_path):
    p = project(deselect=("t/a.py::x", "t/b.py::y"), pytest_extra=("--doctest-modules",))
    ws = rf.Workspace(tmp_path, p)
    assert ws.clone == tmp_path / "clones" / "proj"
    assert ws.venv == tmp_path / "venvs" / "proj"
    assert ws.tool("coverage") == str(tmp_path / "venvs" / "proj" / "bin" / "coverage")
    assert ws.extra_args() == ["--deselect=t/a.py::x", "--deselect=t/b.py::y", "--doctest-modules"]
    assert ws.pytest_args() == [
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        "--deselect=t/a.py::x",
        "--deselect=t/b.py::y",
        "--doctest-modules",
    ]
    assert ws.env()["PATH"].startswith(f"{ws.venv / 'bin'}{os.pathsep}")
    assert rf.Workspace(tmp_path, project()).extra_args() == []


class Recorder:
    """A stand-in for `run.sh` that records every call and answers from a script."""

    def __init__(self, fail=(), on=None):
        self.calls = []
        self.fail = set(fail)  # leading words of the commands that exit 1
        self.on = on or {}  # leading words -> callback(cmd)

    def __call__(self, cmd, cwd, env=None):
        self.calls.append((list(cmd), cwd))
        key = " ".join(cmd[:2])
        if key in self.on:
            self.on[key](cmd)
        return done(cmd, 1 if key in self.fail else 0, "", "bad thing")


def test_prepare_clones_then_builds_the_virtualenv_with_pinned_tools(monkeypatch, tmp_path):
    p = project(requirements=("wcwidth", "extra"))
    ws = rf.Workspace(tmp_path / "ws", p)
    sh = Recorder()
    monkeypatch.setattr(rf, "sh", sh)
    rf.prepare(ws)
    assert sh.calls == [
        (["git", "clone", "-q", "--filter=blob:none", p.url, str(ws.clone)], ws.root),
        (["uv", "venv", "-q", "--python", "3.12", str(ws.venv)], ws.root),
        (
            [
                "uv",
                "pip",
                "install",
                "-q",
                "--python",
                ws.tool("python"),
                "--exclude-newer",
                "2026-10-14T00:00:00Z",
                "pytest==9.1.1",
                "coverage==7.16.2",
                "mutmut==3.8.0",
                "pytest-timeout==2.4.0",
                "wcwidth",
                "extra",
            ],
            ws.root,
        ),
    ]
    assert ws.clone.parent.is_dir()
    assert ws.venv.parent.is_dir()


def test_prepare_reuses_an_existing_clone_and_virtualenv(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path, project())
    ws.clone.mkdir(parents=True)
    ws.venv.mkdir(parents=True)
    sh = Recorder()
    monkeypatch.setattr(rf, "sh", sh)
    rf.prepare(ws)
    assert sh.calls == []
    ws.venv.rmdir()
    rf.prepare(ws)
    assert [c[0][:2] for c in sh.calls] == [["uv", "venv"], ["uv", "pip"]]


def test_prepare_removes_a_half_built_virtualenv_and_reraises(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path, project())
    ws.clone.mkdir(parents=True)
    sh = Recorder(fail={"uv pip"}, on={"uv venv": lambda cmd: Path(cmd[-1]).mkdir(parents=True)})
    monkeypatch.setattr(rf, "sh", sh)
    with pytest.raises(RuntimeError, match=r"^uv pip install failed \(exit 1\): bad thing"):
        rf.prepare(ws)
    assert not ws.venv.exists()


def test_prepare_stops_when_the_clone_fails(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path, project())
    sh = Recorder(fail={"git clone"})
    monkeypatch.setattr(rf, "sh", sh)
    with pytest.raises(RuntimeError, match=r"^clone https://example.invalid/proj failed"):
        rf.prepare(ws)
    assert [c[0][:2] for c in sh.calls] == [["git", "clone"]]
    assert not ws.venv.parent.exists()


def test_prepare_of_a_second_project_reuses_the_shared_directories(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    sh = Recorder()
    monkeypatch.setattr(rf, "sh", sh)
    rf.prepare(rf.Workspace(root, project()))
    rf.prepare(rf.Workspace(root, project(name="other", url="https://example.invalid/other")))
    assert [c[0][:2] for c in sh.calls] == [["git", "clone"], ["uv", "venv"], ["uv", "pip"]] * 2
    assert sorted(path.name for path in root.iterdir()) == ["clones", "venvs"]


def test_prepare_names_a_failing_virtualenv_step(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path, project())
    ws.clone.mkdir(parents=True)
    monkeypatch.setattr(rf, "sh", Recorder(fail={"uv venv"}))
    with pytest.raises(RuntimeError, match=r"^uv venv failed \(exit 1\): bad thing"):
        rf.prepare(ws)


def test_checkout_makes_a_fresh_detached_worktree_each_time(tmp_path, make_repo):
    ws = rf.Workspace(tmp_path / "ws", project())
    up = make_repo(ws.clone)
    one = up.commit({"a.txt": "one\n"})
    two = up.commit({"a.txt": "two\n"})
    tree = rf.checkout(ws, "p", one)
    assert tree == ws.root / "trees" / "proj-p"
    assert (tree / "a.txt").read_text() == "one\n"
    (tree / "leftover.txt").write_text("junk")
    again = rf.checkout(ws, "p", two)
    assert again == tree
    assert (tree / "a.txt").read_text() == "two\n"
    assert not (tree / "leftover.txt").exists()
    assert rf.checkout(ws, "f", one) == ws.root / "trees" / "proj-f"


def test_checkout_of_an_unknown_commit_fails_loudly(tmp_path, make_repo):
    ws = rf.Workspace(tmp_path / "ws", project())
    make_repo(ws.clone).commit({"a.txt": "one\n"})
    with pytest.raises(RuntimeError, match=r"^git worktree add failed"):
        rf.checkout(ws, "p", "0" * 40)


# ----------------------------------------------------------------------------- code structure
def parse_fn(source):
    return ast.parse(source).body[0]


@pytest.mark.parametrize(
    ("source", "span"),
    [
        ('def f():\n    """one line"""\n    return 1\n', (2, 2)),
        ('def f():\n    """two\n\n    more"""\n    return 1\n', (2, 4)),
        ("def f():\n    return 1\n", None),
        ("def f():\n    1\n    return 1\n", None),
        ("def f():\n    g()\n", None),
        ('def f():\n    b"bytes"\n', None),
        ('async def f():\n    """doc"""\n', (2, 2)),
        ('def f():\n    x = "not a docstring"\n', None),
    ],
)
def test_docstring_span(source, span):
    assert rf.docstring_span(parse_fn(source)) == span


def test_functions_of_finds_top_level_functions_and_direct_methods_only():
    assert rf.functions_of(STRUCT) == {
        "top": rf.Function(def_line=6, first=6, last=14, docstring=(7, 10)),
        "Box.make": rf.Function(def_line=21, first=20, last=25, docstring=None),
        "Box.later": rf.Function(def_line=27, first=27, last=28, docstring=None),
        "Box.inner": rf.Function(def_line=30, first=30, last=33, docstring=None),
    }


def test_functions_of_rejects_unparsable_source():
    with pytest.raises(SyntaxError):
        rf.functions_of("def broken(:\n")


def test_statement_spans_are_the_own_lines_of_each_statement():
    source = (
        "class A:\n"
        "    x = 1\n"
        "\n"
        "    def f(self, a,\n"
        "          b):\n"
        '        """d"""\n'
        "        if a:\n"
        "            return (1 +\n"
        "                    2)\n"
        "        return b\n"
    )
    assert sorted(rf.statement_spans(source)) == [
        (1, 1),  # a compound statement is just its header
        (2, 2),
        (4, 5),  # a def header can span lines
        (6, 6),
        (7, 7),
        (8, 9),
        (10, 10),
    ]


def test_statement_spans_of_a_one_line_body_and_of_an_else_clause():
    spans = rf.statement_spans("if a: pass\nif b:\n    pass\nelse:\n    pass\n")
    assert sorted(spans) == [(1, 1), (1, 1), (2, 2), (3, 3), (5, 5)]
    assert rf.statement_of(4, spans) is None  # `else:` belongs to no statement


def test_statement_of_picks_the_innermost_statement_and_is_inclusive():
    spans = [(1, 10), (4, 6), (5, 5)]
    assert rf.statement_of(5, spans) == (5, 5)
    assert rf.statement_of(4, spans) == (4, 6)
    assert rf.statement_of(6, spans) == (4, 6)
    assert rf.statement_of(10, spans) == (1, 10)
    assert rf.statement_of(11, spans) is None
    assert rf.statement_of(0, spans) is None
    assert rf.statement_of(3, []) is None


# ----------------------------------------------------------------------------- diffs
HAND_DIFF = """\
diff --git a/m.py b/m.py
index 111..222 100644
--- a/m.py
+++ b/m.py
@@ -3 +3 @@ def f():
-    x = 1
+    x = 2
@@ -10,2 +10,3 @@
-a
-b
+c
+d
+e
@@ -20,0 +21,2 @@
+new1
+new2
@@ -30,2 +32,0 @@
-gone1
-gone2
"""


def test_changes_reads_each_hunk_of_a_zero_context_diff():
    assert rf.changes(HAND_DIFF) == [
        rf.Change(3, 1, 3, ("    x = 2",)),
        rf.Change(10, 2, 10, ("c", "d", "e")),
        rf.Change(20, 0, 21, ("new1", "new2")),
        rf.Change(30, 2, 32, ()),
    ]


@pytest.mark.parametrize(
    "diff", ["", "\n", "not a diff at all\n+ looks added\n", "@@ garbage @@\n"]
)
def test_changes_of_something_that_is_not_a_diff_is_empty(diff):
    assert rf.changes(diff) == []


def test_changes_does_not_read_the_next_files_header_as_added_text():
    diff = "@@ -1 +1 @@\n-x\n+y\n--- a/n.py\n+++ b/n.py\n@@ -5 +5 @@\n-p\n+q\n"
    assert rf.changes(diff) == [rf.Change(1, 1, 1, ("y",)), rf.Change(5, 1, 5, ("q",))]


def test_changes_keeps_added_text_verbatim_and_skips_the_file_header():
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +1,2 @@\n-x\n+  indented  \n+\n"
    assert rf.changes(diff) == [rf.Change(1, 1, 1, ("  indented  ", ""))]


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("", False),
        ("   ", False),
        ("# c", False),
        ("   # c", False),
        ("x = 1  # c", True),
        ("x", True),
    ],
)
def test_is_code(line, code):
    assert rf.is_code(line) is code


def test_parse_hunks_keeps_context_removed_and_added_lines_per_hunk():
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n keep\n-old\n+new\n"
        "@@ -9 +9 @@\n-x\n+y\n\\ No newline at end of file\n"
    )
    assert rf.parse_hunks(diff) == [
        [(" ", "keep"), ("-", "old"), ("+", "new")],
        [("-", "x"), ("+", "y")],
    ]
    assert rf.parse_hunks("") == []
    assert rf.parse_hunks("--- a\n+++ b\n") == []


LINES = ["a", "def f():", "    x = 1", "    y = 2", "    return x"]


def test_place_matches_the_text_not_the_line_numbers():
    hunk = [(" ", "def f():"), ("-", "x = 1"), ("+", "x = 2")]
    assert rf.place(hunk, LINES, 2, 5) == (1, [3])
    assert rf.place([("-", "y = 2"), ("-", "return x")], LINES, 2, 5) == (3, [4, 5])
    assert rf.place([("-", "no such line")], LINES, 2, 5) is None


def test_place_searches_from_thirteen_lines_above_the_function_up_to_its_last_line():
    lines = [f"line{i}" for i in range(1, 41)]
    hunk = [("-", "line20")]
    # `line20` is index 19: the first index searched is lo - 13, the last is hi - 1
    assert rf.place(hunk, lines, 32, 40) == (19, [20])
    assert rf.place(hunk, lines, 33, 40) is None
    assert rf.place(hunk, lines, 10, 20) == (19, [20])
    assert rf.place(hunk, lines, 10, 19) is None


def test_place_returns_the_first_of_several_matches_and_skips_a_short_tail():
    lines = ["x", "y", "x", "y", "x"]
    assert rf.place([("-", "x"), (" ", "y")], lines, 1, 5) == (0, [1])
    assert rf.place([(" ", "y"), ("-", "x")], lines, 1, 5) == (1, [3])
    # a window that runs off the end of the file never matches
    assert rf.place([(" ", "x"), ("-", "y")], ["a", "x"], 1, 2) is None
    assert rf.place([(" ", "x"), ("-", "y")], ["a", "x", "y"], 1, 3) == (1, [3])


def test_apply_hunks_replaces_old_lines_with_new_ones_bottom_up():
    lines = ["a", "b", "c", "d", "e"]
    first = [(" ", "a"), ("-", "b"), ("+", "B1"), ("+", "B2")]
    second = [("-", "d"), ("+", "D")]
    assert rf.apply_hunks(lines, [(first, 0), (second, 3)]) == ["a", "B1", "B2", "c", "D", "e"]
    assert rf.apply_hunks(lines, [(second, 3), (first, 0)]) == ["a", "B1", "B2", "c", "D", "e"]
    assert lines == ["a", "b", "c", "d", "e"]  # the input is not modified
    assert rf.apply_hunks(lines, []) == lines
    assert rf.apply_hunks(lines, [([("-", "e")], 4)]) == ["a", "b", "c", "d"]


def test_canon_drops_blanks_comments_and_layout():
    assert rf.canon(["  x  =  1  # c", "", "   # only", "y\t=\t2"]) == ["x = 1", "y = 2"]
    assert rf.canon([]) == []


def test_fix_size_counts_removed_and_added_code_lines_in_the_buggy_functions():
    changed = CHECK.replace("hi - 1", "hi")
    changed = changed.replace("Clamp v into [lo, hi].", "Clamp it.")
    changed = changed.replace("# bounds first", "# first, the bounds")
    assert rf.fix_size(udiff(CHECK, changed), CHECK, changed, ("check",)) == 2


def test_fix_size_of_an_inserted_guard_counts_only_its_code_lines():
    guarded = CHECK.replace(
        "    # bounds first\n",
        "    # bounds first\n    # explain\n    if lo > hi:\n        raise ValueError(lo)\n",
    )
    assert rf.fix_size(udiff(CHECK, guarded), CHECK, guarded, ("check",)) == 2


def test_fix_size_adds_up_every_hunk_and_every_buggy_function():
    two_hunks = CHECK.replace("return lo\n", "return lo + 1\n").replace("hi - 1", "hi")
    assert rf.fix_size(udiff(CHECK, two_hunks), CHECK, two_hunks, ("check",)) == 4
    two_functions = STRUCT.replace("return a", "return b").replace("range(n)", "range(n + 1)")
    diff = udiff(STRUCT, two_functions)
    assert rf.fix_size(diff, STRUCT, two_functions, ("top", "Box.make")) == 4
    assert rf.fix_size(diff, STRUCT, two_functions, ("Box.make",)) == 2


def test_fix_size_does_not_count_lines_outside_the_function_or_in_its_docstring():
    import_changed = CHECK.replace("import math", "import os")
    assert rf.fix_size(udiff(CHECK, import_changed), CHECK, import_changed, ("check",)) == 0
    # `top`'s docstring spans lines 7-10: editing any of its lines is not a code change
    doc_changed = STRUCT.replace("Doc one.", "Doc 1.").replace("More doc.", "More.")
    assert rf.fix_size(udiff(STRUCT, doc_changed), STRUCT, doc_changed, ("top",)) == 0


def test_fix_size_counts_the_def_line_of_a_function_that_starts_on_line_one():
    old, new = "def f(x):\n    return x\n", "def f(x, y=1):\n    return x\n"
    assert rf.fix_size(udiff(old, new), old, new, ("f",)) == 2


def test_fix_size_ignores_other_functions_and_unknown_names():
    changed = CHECK.replace("return 0", "return 1")
    diff = udiff(CHECK, changed)
    assert rf.fix_size(diff, CHECK, changed, ("check",)) == 0
    assert rf.fix_size(diff, CHECK, changed, ("other",)) == 2
    assert rf.fix_size(diff, CHECK, changed, ("missing",)) == 0
    assert rf.fix_size("", CHECK, CHECK, ("check",)) == 0


# ----------------------------------------------------------------------------- faulty lines
def test_faulty_lines_of_a_rewrite_ignore_docstring_and_comment_edits():
    fixed = FIXED_CHECK.replace("Clamp v into [lo, hi].", "Clamp it.")
    fixed = fixed.replace("# bounds first", "# check the bounds first")
    fault = rf.faulty_lines(CHECK, udiff(CHECK, fixed), ("check",))
    assert fault == rf.Fault("modified", [10], [10], ["check"])


def test_faulty_lines_of_an_inserted_guard_are_the_statements_around_it():
    guarded = CHECK.replace(
        "    # bounds first\n",
        "    # bounds first\n    if lo > hi:\n        raise ValueError(lo)\n",
    )
    fault = rf.faulty_lines(CHECK, udiff(CHECK, guarded), ("check",))
    # inserted after line 6: the docstring (5) before it and the `if` (7) after it stand in
    assert fault == rf.Fault("omission", [5, 7], [5, 7], ["check"])


def inserted(text, *places):
    """`text` with each (after_line, new_line) inserted, numbering lines as in `text`."""
    lines = text.splitlines(keepends=True)
    for after, new in sorted(places, reverse=True):
        lines.insert(after, new + "\n")
    return "".join(lines)


@pytest.mark.parametrize(
    ("places", "lines"),
    [
        ([(8, "    z = 1")], [8, 9]),  # after a statement: it and the next one
        ([(4, "    z = 1")], [4, 5]),  # right after the `def` line
        ([(11, "    z = 1")], [11]),  # after the last line: nothing follows
        ([(8, "    z = 1"), (10, "    y = 2")], [8, 9, 10, 11]),  # several insertions add up
    ],
)
def test_faulty_lines_of_insertions_anchor_on_the_statements_on_either_side(places, lines):
    fixed = inserted(CHECK, *places)
    fault = rf.faulty_lines(CHECK, udiff(CHECK, fixed), ("check",))
    assert fault == rf.Fault("omission", lines, lines, ["check"])


def test_faulty_lines_of_an_insertion_next_to_a_multi_line_statement():
    fixed = inserted(MULTI, (1, "    z = 1"))
    fault = rf.faulty_lines(MULTI, udiff(MULTI, fixed), ("build",))
    assert fault == rf.Fault("omission", [1, 2], [1, 2, 3, 4, 5], ["build"])


def test_faulty_lines_of_an_insertion_after_a_decorator_have_no_statement_before_it():
    fixed = inserted(DECORATED, (1, "# note\nx = 1"))
    fault = rf.faulty_lines(DECORATED, udiff(DECORATED, fixed), ("cached",))
    assert fault == rf.Fault("omission", [2], [2], ["cached"])


def test_faulty_lines_include_the_last_line_of_the_function_but_not_docstring_lines():
    fixed = CHECK.replace("    return v\n", "    return v + 0\n")
    assert rf.faulty_lines(CHECK, udiff(CHECK, fixed), ("check",)) == rf.Fault(
        "modified", [11], [11], ["check"]
    )
    # `top`'s docstring spans lines 7-10: only the real code change counts
    both = STRUCT.replace("Doc one.", "Doc 1.").replace("return a", "return b")
    assert rf.faulty_lines(STRUCT, udiff(STRUCT, both), ("top",)) == rf.Fault(
        "modified", [13], [13], ["top"]
    )


def test_faulty_lines_of_code_replacing_a_comment_is_an_omission_too():
    replaced = CHECK.replace("    # bounds first\n", "    lo = max(lo, 0)\n")
    fault = rf.faulty_lines(CHECK, udiff(CHECK, replaced), ("check",))
    assert fault == rf.Fault("omission", [5, 7], [5, 7], ["check"])


@pytest.mark.parametrize(
    "edited",
    [
        CHECK.replace("    # bounds first\n", "    # bounds first\n    # and more\n"),
        CHECK.replace("return 0", "return 1"),
        CHECK + "\n\ndef extra():\n    return 1\n",
        CHECK,
    ],
    ids=["comment-only", "other-function", "after-the-function", "no-change"],
)
def test_faulty_lines_outside_the_buggy_code_find_nothing(edited):
    diff = udiff(CHECK, edited) if edited != CHECK else ""
    assert rf.faulty_lines(CHECK, diff, ("check",)) == rf.Fault("omission", [], [], ["check"])


def test_faulty_lines_expand_a_multi_line_statement_to_all_its_lines():
    fixed = MULTI.replace("        a,\n", "        b,\n")
    fault = rf.faulty_lines(MULTI, udiff(MULTI, fixed), ("build",))
    assert fault == rf.Fault("modified", [3], [2, 3, 4, 5], ["build"])


def test_faulty_lines_count_a_decorator_as_part_of_the_function():
    fixed = DECORATED.replace("@cache", "@cache(1)")
    fault = rf.faulty_lines(DECORATED, udiff(DECORATED, fixed), ("cached",))
    assert fault == rf.Fault("modified", [1], [1], ["cached"])


def test_faulty_lines_over_several_functions_and_methods():
    fixed = STRUCT.replace("        return a\n", "        return b\n")
    fixed = fixed.replace("range(n)", "range(n + 1)")
    fault = rf.faulty_lines(STRUCT, udiff(STRUCT, fixed), ("top", "Box.make"))
    assert fault == rf.Fault("modified", [13, 23], [13, 23], ["top", "Box.make"])
    assert fault.fix_size == 0


def test_faulty_lines_fail_loudly_on_a_function_the_source_does_not_have():
    with pytest.raises(KeyError, match="nope"):
        rf.faulty_lines(CHECK, udiff(CHECK, FIXED_CHECK), ("nope",))


def test_faulty_lines_fail_loudly_on_unparsable_source():
    with pytest.raises(SyntaxError):
        rf.faulty_lines("def (:\n", "@@ -1 +1 @@\n-a\n+b\n", ("f",))


# ----------------------------------------------------------------------------- verify helpers
@pytest.mark.parametrize(
    ("expected", "failing", "answer"),
    [
        ((), {"t.py::a"}, False),  # nothing to check is not a pass
        (("t.py::a",), {"t.py::a"}, True),
        (("t.py::a",), {"t.py::a[1-2]", "t.py::a[3]"}, True),  # a parametrized family
        (("t.py::a",), {"t.py::ab"}, False),  # a prefix is not a family
        (("t.py::a",), {"t.py::a_other", "t.py::b"}, False),
        (("t.py::a", "t.py::b"), {"t.py::a"}, False),  # every listed test must fail
        (("t.py::a", "t.py::b"), {"t.py::a", "t.py::b[x]", "t.py::c"}, True),
        (("t.py::a",), set(), False),
    ],
)
def test_names_failing(expected, failing, answer):
    assert rf.names_failing(expected, failing) is answer


def test_test_fail_pattern_reads_pytest_short_summaries():
    out = (
        "FAILED tests/t.py::test_a - assert 1 == 2\n"
        "ERROR tests/t.py::test_b\n"
        "SUBFAILED(x=1, y=2) tests/t.py::test_c[p] - boom\n"
        "PASSED tests/t.py::test_d\n"
        "  FAILED tests/t.py::indented\n"
        "FAILED tests/t.py - collection error\n"
    )
    assert set(rf.TEST_FAIL.findall(out)) == {
        "tests/t.py::test_a",
        "tests/t.py::test_b",
        "tests/t.py::test_c[p]",
    }


def test_pytest_failures_runs_the_projects_python_and_reports_the_failing_tests(
    monkeypatch, tmp_path
):
    ws = rf.Workspace(tmp_path / "ws", project(pytest_extra=("--doctest-modules",)))
    calls = []

    def fake(cmd, cwd, env=None):
        calls.append((cmd, cwd, env))
        return done(cmd, 1, "FAILED t/a.py::x - no\nERROR t/b.py::y\n3 failed\n")

    monkeypatch.setattr(rf, "sh", fake)
    tree = tmp_path / "tree"
    assert rf.pytest_failures(ws, tree, ["t/a.py", "t/b.py"]) == (1, {"t/a.py::x", "t/b.py::y"})
    cmd, cwd, env = calls[0]
    assert cmd == [
        ws.tool("python"),
        "-m",
        "pytest",
        *ws.pytest_args(),
        "--timeout=120",
        "-rfE",
        "--tb=no",
        "t/a.py",
        "t/b.py",
    ]
    assert cwd == tree
    assert env == ws.env()


def test_changed_tests_lists_changed_python_files_under_the_test_paths(tmp_path, make_repo):
    ws = rf.Workspace(tmp_path / "ws", project())
    up = make_repo(ws.clone)
    files = {
        "pkg/m.py": "1\n",
        "tests/test_a.py": "1\n",
        "tests/sub/test_c.py": "1\n",
        "tests/data.txt": "1\n",
        "other/test_b.py": "1\n",
    }
    parent = up.commit(files)
    fix = up.commit(dict.fromkeys(files, "2\n"))
    up.commit({"tests/test_late.py": "3\n", "tests/test_a.py": "3\n"})  # after F: not F's
    b = bug(parent=parent, fix=fix)
    assert rf.changed_tests(ws, b) == ["tests/sub/test_c.py", "tests/test_a.py"]
    assert rf.changed_tests(ws, bug(parent=fix, fix=fix)) == []


@pytest.fixture
def make_repo(repo):
    def make(path):
        path.mkdir(parents=True)
        return type(repo)(path)

    return make


def test_verify_runs_f_s_tests_on_p_and_on_f(monkeypatch, tmp_path, make_repo):
    ws = rf.Workspace(tmp_path / "ws", project())
    up = make_repo(ws.clone)
    parent = up.commit({"pkg/mod.py": CHECK, "tests/test_mod.py": TEST_P})
    fix = up.commit({"pkg/mod.py": FIXED_CHECK, "tests/test_mod.py": TEST_F})
    seen = []
    script = {
        "proj-verify-p": (1, {"tests/test_mod.py::test_high"}),
        "proj-verify-f": (0, set()),
    }

    def fake(w, tree, targets):
        seen.append(
            (
                tree.name,
                targets,
                (tree / "tests/test_mod.py").read_text(),
                (tree / "pkg/mod.py").read_text(),
            )
        )
        return script[tree.name]

    monkeypatch.setattr(rf, "pytest_failures", fake)
    result = rf.verify(ws, bug(parent=parent, fix=fix))
    assert result == {
        "changed_test_files": ["tests/test_mod.py"],
        "failing_at_parent": ["tests/test_mod.py::test_high"],
        "failing_at_fix": [],
        "regression_fails_at_parent": True,
        "regression_passes_at_fix": True,
    }
    # at P the buggy source runs against F's version of the test files; at F everything is F's
    assert seen == [
        ("proj-verify-p", ["tests/test_mod.py"], TEST_F, CHECK),
        ("proj-verify-f", ["tests/test_mod.py"], TEST_F, FIXED_CHECK),
    ]


@pytest.mark.parametrize(
    ("at_parent", "at_fix", "fails_at_p", "passes_at_f"),
    [
        ((0, set()), (0, set()), False, True),  # the test does not catch the bug
        ((1, {"tests/test_mod.py::other"}), (0, set()), False, True),  # wrong test fails
        ((1, {"tests/test_mod.py::test_high"}), (1, {"tests/test_mod.py::x"}), True, False),
    ],
)
def test_verify_reports_a_regression_test_that_does_not_behave(
    monkeypatch, tmp_path, make_repo, at_parent, at_fix, fails_at_p, passes_at_f
):
    ws = rf.Workspace(tmp_path / "ws", project())
    up = make_repo(ws.clone)
    parent = up.commit({"tests/test_mod.py": TEST_P})
    fix = up.commit({"tests/test_mod.py": TEST_F})
    script = {"proj-verify-p": at_parent, "proj-verify-f": at_fix}
    monkeypatch.setattr(rf, "pytest_failures", lambda w, tree, targets: script[tree.name])
    result = rf.verify(ws, bug(parent=parent, fix=fix))
    assert result["regression_fails_at_parent"] is fails_at_p
    assert result["regression_passes_at_fix"] is passes_at_f


# ----------------------------------------------------------------------------- coverage
def test_coverage_verdict_of_a_faulty_line_no_test_ran():
    fault = rf.Fault("modified", [10], [10], ["check"])
    entry = {
        "executed_lines": [1, 4, 7, 8, 9, 11, 14, 15],
        "missing_lines": [10],
        "missing_branches": [[9, 10]],
    }
    assert rf.coverage_verdict(fault, CHECK, entry) == {
        "executable_faulty_lines": [10],
        "executed": [],
        "not_executed": [10],
        "partial_branch_lines": [],
        "percent": 0.0,
        "all_executed": False,
        "diff_coverage_gate": "block",
    }


def test_coverage_verdict_of_executed_lines_with_a_partial_branch():
    fault = rf.Fault("modified", [9, 10], [9, 10], ["check"])
    entry = {
        "executed_lines": [1, 4, 7, 8, 9, 10, 11, 14, 15],
        "missing_lines": [],
        "missing_branches": [[9, 11], [7, 11]],
    }
    verdict = rf.coverage_verdict(fault, CHECK, entry)
    assert verdict == {
        "executable_faulty_lines": [9, 10],
        "executed": [9, 10],
        "not_executed": [],
        "partial_branch_lines": [9],
        "percent": 100.0,
        "all_executed": True,
        "diff_coverage_gate": "pass",
    }


def test_coverage_verdict_maps_lines_to_statements_and_drops_non_executable_ones():
    # 5 is the docstring and 6 a comment: neither is in coverage's lists, so they do not count
    fault = rf.Fault("omission", [5, 6, 10], [5, 6, 10], ["check"])
    entry = {"executed_lines": [10], "missing_lines": [7]}
    verdict = rf.coverage_verdict(fault, CHECK, entry)
    assert verdict["executable_faulty_lines"] == [10]
    assert verdict["partial_branch_lines"] == []  # `missing_branches` is optional
    multi = rf.Fault("modified", [3], [2, 3, 4, 5], ["build"])
    entry = {"executed_lines": [1, 2, 6], "missing_lines": []}
    got = rf.coverage_verdict(multi, MULTI, entry)
    assert (got["executable_faulty_lines"], got["executed"]) == ([2], [2])


def test_coverage_verdict_without_executable_faulty_lines_blocks():
    fault = rf.Fault("omission", [], [], ["check"])
    verdict = rf.coverage_verdict(fault, CHECK, {"executed_lines": [1], "missing_lines": [4]})
    assert verdict == {
        "executable_faulty_lines": [],
        "executed": [],
        "not_executed": [],
        "partial_branch_lines": [],
        "percent": None,
        "all_executed": False,
        "diff_coverage_gate": "block",
    }


@pytest.mark.parametrize(
    ("statements", "executed", "percent", "gate"),
    [
        (10, 10, 100.0, "pass"),
        (10, 9, 90.0, "pass"),  # the gate is "at least 90"
        (10, 8, 80.0, "block"),
        (9, 8, 88.9, "block"),
        (3, 2, 66.7, "block"),
    ],
)
def test_coverage_verdict_gate_boundary(statements, executed, percent, gate):
    text = "".join(f"v{i} = {i}\n" for i in range(1, statements + 1))
    lines = list(range(1, statements + 1))
    entry = {"executed_lines": lines[:executed], "missing_lines": lines[executed:]}
    verdict = rf.coverage_verdict(rf.Fault("modified", lines, lines, []), text, entry)
    assert (verdict["percent"], verdict["diff_coverage_gate"]) == (percent, gate)
    assert verdict["all_executed"] is (executed == statements)


def test_coverage_verdict_fails_loudly_on_a_malformed_report_entry():
    with pytest.raises(KeyError, match="missing_lines"):
        rf.coverage_verdict(rf.Fault("modified", [1], [1], []), "x = 1\n", {"executed_lines": [1]})


def coverage_tree(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "mod.py").write_text(CHECK, encoding="utf-8")
    return tree


def test_coverage_of_runs_the_suite_under_coverage_and_judges_the_report(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path / "ws", project(run_paths=("tests", "pkg")))
    tree = coverage_tree(tmp_path)
    calls = []
    report = {
        "files": {"pkg/mod.py": {"executed_lines": [1, 4, 7, 8, 9, 10, 11], "missing_lines": []}}
    }

    def fake(cmd, cwd, env=None):
        calls.append((cmd, cwd, env))
        if cmd[1] == "json":
            Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return done(cmd)

    monkeypatch.setattr(rf, "sh", fake)
    fault = rf.Fault("modified", [10], [10], ["check"])
    verdict = rf.coverage_of(ws, bug(), fault, tree)
    assert verdict == rf.coverage_verdict(fault, CHECK, report["files"]["pkg/mod.py"])
    assert verdict["all_executed"] is True
    run, json_cmd = calls
    assert run[0] == [
        ws.tool("coverage"), "run", "--branch", "--include=pkg/mod.py", "-m", "pytest",
        *ws.pytest_args(), "tests", "pkg",
    ]  # fmt: skip
    assert json_cmd[0] == [ws.tool("coverage"), "json", "-q", "-o", str(tree / "coverage.rf.json")]
    for _, cwd, env in calls:
        assert cwd == tree
        assert env["COVERAGE_FILE"] == str(tree / ".coverage.rf")
        assert env["PATH"].startswith(str(ws.venv / "bin"))


def test_coverage_of_defaults_to_the_test_paths(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path / "ws", project(test_paths=("tests", "more")))
    tree = coverage_tree(tmp_path)
    calls = []

    def fake(cmd, cwd, env=None):
        calls.append(cmd)
        if cmd[1] == "json":
            entry = {"executed_lines": [10], "missing_lines": []}
            Path(cmd[-1]).write_text(json.dumps({"files": {"pkg/mod.py": entry}}))
        return done(cmd)

    monkeypatch.setattr(rf, "sh", fake)
    rf.coverage_of(ws, bug(), rf.Fault("modified", [10], [10], []), tree)
    assert calls[0][-2:] == ["tests", "more"]


def test_coverage_of_refuses_a_parent_whose_own_suite_fails(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path / "ws", project())
    monkeypatch.setattr(rf, "sh", lambda cmd, cwd, env=None: done(cmd, 1, "z" * 3000 + "TAIL"))
    with pytest.raises(RuntimeError) as raised:
        rf.coverage_of(ws, bug(), rf.Fault("modified", [10], [10], []), coverage_tree(tmp_path))
    assert str(raised.value) == "P's own suite does not pass here: " + "z" * 996 + "TAIL"


def test_coverage_of_fails_loudly_when_the_report_cannot_be_produced_or_read(monkeypatch, tmp_path):
    ws = rf.Workspace(tmp_path / "ws", project())
    tree = coverage_tree(tmp_path)
    fault = rf.Fault("modified", [10], [10], [])
    monkeypatch.setattr(rf, "sh", lambda cmd, cwd, env=None: done(cmd, int(cmd[1] == "json")))
    with pytest.raises(RuntimeError, match=r"^coverage json failed"):
        rf.coverage_of(ws, bug(), fault, tree)

    def no_report(cmd, cwd, env=None):
        return done(cmd)

    monkeypatch.setattr(rf, "sh", no_report)
    with pytest.raises(FileNotFoundError):
        rf.coverage_of(ws, bug(), fault, tree)

    def wrong_file(cmd, cwd, env=None):
        if cmd[1] == "json":
            Path(cmd[-1]).write_text('{"files": {"elsewhere.py": {}}}')
        return done(cmd)

    monkeypatch.setattr(rf, "sh", wrong_file)
    with pytest.raises(KeyError, match=r"pkg/mod\.py"):
        rf.coverage_of(ws, bug(), fault, tree)

    def garbage(cmd, cwd, env=None):
        if cmd[1] == "json":
            Path(cmd[-1]).write_text("not json")
        return done(cmd)

    monkeypatch.setattr(rf, "sh", garbage)
    with pytest.raises(json.JSONDecodeError):
        rf.coverage_of(ws, bug(), fault, tree)


# ----------------------------------------------------------------------------- mutation setup
def test_write_mutmut_config_appends_a_section_to_the_scratch_trees_setup_cfg(tmp_path):
    p = project(
        source_paths=("src/", "lib/"),
        pytest_extra=("--no-cov",),
        deselect=("t.py::x",),
        also_copy=("README.md", "docs/"),
        mutmut_config=("process_isolation = forkserver",),
    )
    ws = rf.Workspace(tmp_path / "ws", p)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "setup.cfg").write_text("[metadata]\nname = x\n\n\n", encoding="utf-8")
    rf.write_mutmut_config(ws, bug(file="src/m.py"), tree)
    assert (tree / "setup.cfg").read_text(encoding="utf-8") == (
        "[metadata]\nname = x\n"
        "\n[mutmut]\n"
        "source_paths = src/ lib/\n"
        "only_mutate = src/m.py\n"
        "pytest_add_cli_args =\n"
        "    -p\n"
        "    no:randomly\n"
        "    -x\n"
        "    -q\n"
        "    --deselect=t.py::x\n"
        "    --no-cov\n"
        "also_copy =\n"
        "    README.md\n"
        "    docs/\n"
        "process_isolation = forkserver\n"
    )


def test_write_mutmut_config_creates_the_file_and_omits_empty_options(tmp_path):
    ws = rf.Workspace(tmp_path / "ws", project())
    tree = tmp_path / "tree"
    tree.mkdir()
    rf.write_mutmut_config(ws, bug(), tree)
    assert (tree / "setup.cfg").read_text(encoding="utf-8") == (
        "\n\n[mutmut]\nsource_paths = pkg/\nonly_mutate = pkg/mod.py\n"
        "pytest_add_cli_args =\n    -p\n    no:randomly\n    -x\n    -q\n"
    )


@pytest.mark.parametrize("existing", ["a = 1 \t\n\n", "a = XX\n", "a = 1\n\n\n\n"])
def test_write_mutmut_config_keeps_the_existing_text_but_for_trailing_newlines(tmp_path, existing):
    ws = rf.Workspace(tmp_path / "ws", project())
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "setup.cfg").write_text(existing, encoding="utf-8")
    rf.write_mutmut_config(ws, bug(), tree)
    written = (tree / "setup.cfg").read_text(encoding="utf-8")
    assert written.startswith(existing.rstrip("\n") + "\n\n[mutmut]\n")


def test_selectors_name_the_functions_the_fix_changes():
    diff = udiff(SEL_SRC, SEL_SRC.replace("x + 1", "x + 2"))
    b = bug(functions=("Stats.calc",))
    assert rf.selectors(b, SEL_SRC, diff) == ["pkg.mod.xǁStatsǁcalc__mutmut_*"]


def test_selectors_follow_the_file_order_and_cover_top_level_functions():
    fixed = SEL_SRC.replace("x + 1", "x + 2").replace("return y", "return -y")
    b = bug(functions=("helper", "Stats.calc"))
    assert rf.selectors(b, SEL_SRC, udiff(SEL_SRC, fixed)) == [
        "pkg.mod.xǁStatsǁcalc__mutmut_*",
        "pkg.mod.x_helper__mutmut_*",
    ]


def test_selectors_of_a_one_line_change_stop_at_that_line():
    tight = "def a():\n    return 1\ndef b():\n    return 2\n"
    diff = udiff(tight, tight.replace("return 1", "return 9"))
    assert rf.selectors(bug(functions=("a",)), tight, diff) == ["pkg.mod.x_a__mutmut_*"]


def test_selectors_use_the_import_name_of_a_src_layout_module():
    b = bug(file="src/pkg/mod.py", functions=("helper",))
    diff = udiff(SEL_SRC, SEL_SRC.replace("return y", "return -y"))
    assert rf.selectors(b, SEL_SRC, diff) == ["pkg.mod.x_helper__mutmut_*"]


def test_selectors_select_a_function_for_an_insertion_inside_it():
    diff = "@@ -9,0 +10 @@\n+    y = 1\n"  # inserted after line 9, the `def helper` line
    assert rf.selectors(bug(functions=("helper",)), SEL_SRC, diff) == ["pkg.mod.x_helper__mutmut_*"]


@pytest.mark.parametrize(
    ("functions", "diff"),
    [
        (("helper",), udiff(SEL_SRC, SEL_SRC.replace("x + 1", "x + 2"))),  # touches calc
        (("Stats.calc", "helper"), udiff(SEL_SRC, SEL_SRC.replace("x + 1", "x + 2"))),
        (("Stats.calc",), ""),  # an empty or unparsable diff selects nothing
        (("Stats.calc",), "garbage\n"),
        (("Stats.calc",), "@@ -8,0 +9 @@\n+# between the functions\n"),
    ],
)
def test_selectors_refuse_a_diff_that_disagrees_with_the_manifest(functions, diff):
    with pytest.raises(RuntimeError, match="do not match the manifest's functions"):
        rf.selectors(bug(functions=functions), SEL_SRC, diff)


# ----------------------------------------------------------------------------- scripted tools
FAILING_AT_PARENT = "FAILED tests/test_mod.py::test_high - assert 4 == 5\n1 failed\n"
P_STATS = {"killed": 3, "survived": 1, "no_tests": 0, "timeout": 0, "suspicious": 0,
           "segfault": 0, "skipped": 0, "total": 4}  # fmt: skip
F_STATS = {**P_STATS, "killed": 4, "survived": 0, "total": 4}
LISTING_P = (
    "Legend: killed, survived, no tests\n"
    "    pkg.mod.x_check__mutmut_1: killed\n"
    "    pkg.mod.x_check__mutmut_2: survived\n"
    "    pkg.mod.x_other__mutmut_1: survived\n"
)
LISTING_F = "    pkg.mod.x_check__mutmut_1: killed\n    pkg.mod.x_check__mutmut_2: killed\n"
MUTANT_2_DIFF = (
    "--- pkg/mod.py\n+++ pkg/mod.py\n@@ -1,5 +1,5 @@\n"
    "     if v > hi:\n-        return hi - 1\n+        return hi\n     return v\n"
)
SHOWN = {"pkg.mod.x_check__mutmut_2": "# pkg.mod.x_check__mutmut_2: survived\n" + MUTANT_2_DIFF}
COVERAGE = {
    "files": {
        "pkg/mod.py": {
            "executed_lines": [1, 4, 7, 8, 9, 10, 11, 14, 15],
            "missing_lines": [],
            "missing_branches": [],
        }
    }
}
GATE_P = (
    "#### Mutation testing\n\nScore **75%** (minimum 80%): 3 killed, 0 timed out, 1 survived.\n"
)
GATE_F = "#### Mutation testing\n\nScore **100%** (minimum 80%): 4 killed, 0 timed out.\n"


class Tools:
    """Scripted stand-ins for uv, pytest, coverage, mutmut and `golden mutation`.

    Installed in place of `run.sh`: git commands run for real (against local repositories),
    everything else answers from a script, and a command nobody scripted fails the test.
    """

    def __init__(self, real_sh):
        self.real_sh = real_sh
        self.calls = []
        self.codes = {}  # "tool subcommand" -> exit code (default 0)
        self.pytest = {"verify-p": (1, FAILING_AT_PARENT), "verify-f": (0, "")}
        self.pytest_seen = []
        self.coverage = copy.deepcopy(COVERAGE)
        self.stats = {"p": dict(P_STATS), "f": dict(F_STATS)}
        self.listing = {"p": LISTING_P, "f": LISTING_F}
        self.shown = dict(SHOWN)
        self.gate = {"p": (1, GATE_P), "f": (0, GATE_F)}
        self.real_gate = False
        self.run_out = "running mutants\n"

    def named(self, tool, sub=None):
        return [
            c for c, _ in self.calls if Path(c[0]).name == tool and (sub is None or c[1] == sub)
        ]

    @staticmethod
    def label(cwd):
        return cwd.name.removeprefix("proj-")

    def __call__(self, cmd, cwd, env=None):
        self.calls.append((list(cmd), cwd))
        if cmd[1:3] == ["-m", "golden"]:
            if self.real_gate:
                return self.real_sh(cmd, cwd, env)
            code, out = self.gate[self.label(cwd)]
            return done(cmd, self.codes.get("gate", code), out)
        tool = Path(cmd[0]).name
        if tool == "git":
            return self.real_sh(cmd, cwd, env)
        return getattr(self, f"_{tool}")(cmd, cwd)

    def _uv(self, cmd, cwd):
        code = self.codes.get(f"uv {cmd[1]}", 0)
        if cmd[1] == "venv" and code == 0:
            Path(cmd[-1]).mkdir(parents=True)
        return done(cmd, code)

    def _python(self, cmd, cwd):
        assert cmd[1:3] == ["-m", "pytest"], cmd
        label = self.label(cwd)
        self.pytest_seen.append((label, (cwd / "tests" / "test_mod.py").read_text()))
        code, out = self.pytest[label]
        return done(cmd, code, out)

    def _coverage(self, cmd, cwd):
        code = self.codes.get(f"coverage {cmd[1]}", 0)
        if cmd[1] == "json" and code == 0:
            report = Path(cmd[cmd.index("-o") + 1])
            report.write_text(json.dumps(self.coverage), encoding="utf-8")
        return done(cmd, code, "suite output\n")

    def _mutmut(self, cmd, cwd):
        sub, label = cmd[1], self.label(cwd)
        code = self.codes.get(f"mutmut {sub}", 0)
        out = err = ""
        if sub == "run":
            out, err = self.run_out, "a warning\n"
        elif sub == "export-cicd-stats" and code == 0:
            stats = cwd / "mutants" / "mutmut-cicd-stats.json"
            stats.parent.mkdir(exist_ok=True)
            stats.write_text(json.dumps(self.stats[label]), encoding="utf-8")
        elif sub == "results":
            out = self.listing[label]
        elif sub == "show":
            out = self.shown[cmd[2]]
        return done(cmd, code, out, err)


@pytest.fixture
def tools(monkeypatch):
    scripted = Tools(rf.sh)
    monkeypatch.setattr(rf, "sh", scripted)
    return scripted


@pytest.fixture
def tree(tmp_path):
    """A scratch tree named like the runner's, holding the buggy source."""
    path = tmp_path / "ws" / "trees" / "proj-p"
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "mod.py").write_text(CHECK, encoding="utf-8")
    return path


@pytest.fixture
def ws(tmp_path):
    return rf.Workspace(tmp_path / "ws", project())


@pytest.mark.parametrize(
    ("code", "verdict", "score"),
    [(0, "pass", 84.62), (1, "block", 40.0), (2, "error", None), (-9, "error", None)],
)
def test_gate_verdict_reads_the_exit_code_and_the_score(
    monkeypatch, tmp_path, code, verdict, score
):
    calls = []

    def fake(cmd, cwd, env=None):
        calls.append((cmd, cwd, env))
        out = f"Score **{score:g}%** (minimum 80%)" if score is not None else "usage: golden"
        return done(cmd, code, out)

    monkeypatch.setattr(rf, "sh", fake)
    env = {"PATH": "/bin"}
    assert rf.gate_verdict(tmp_path, env) == (verdict, code, score)
    cmd, cwd, passed = calls[0]
    assert cmd == [sys.executable, "-m", "golden", "mutation", "mutants/mutmut-cicd-stats.json",
                   "--format", "mutmut"]  # fmt: skip
    assert cwd == tmp_path
    assert passed == {"PATH": "/bin", "PYTHONPATH": str(ROOT / "src")}


def test_gate_verdict_finds_the_score_on_either_stream_or_none(monkeypatch, tmp_path):
    outputs = iter([done(code=0, err="Score **91.5%** ok"), done(code=0, out="no score here")])
    monkeypatch.setattr(rf, "sh", lambda cmd, cwd, env=None: next(outputs))
    assert rf.gate_verdict(tmp_path, {}) == ("pass", 0, 91.5)
    assert rf.gate_verdict(tmp_path, {}) == ("pass", 0, None)


@pytest.mark.parametrize(
    ("killed", "survived", "score", "verdict", "code"),
    [(8, 2, 80.0, "pass", 0), (7, 3, 70.0, "block", 1), (10, 0, 100.0, "pass", 0)],
)
def test_gate_verdict_against_the_real_golden_mutation_gate(
    tmp_path, killed, survived, score, verdict, code
):
    stats = tmp_path / "mutants" / "mutmut-cicd-stats.json"
    stats.parent.mkdir()
    stats.write_text(
        json.dumps({"killed": killed, "survived": survived, "no_tests": 0, "timeout": 0,
                    "suspicious": 0, "segfault": 0}),
        encoding="utf-8",
    )  # fmt: skip
    assert rf.gate_verdict(tmp_path, rf.clean_env()) == (verdict, code, score)


def test_gate_verdict_errors_when_the_real_gate_cannot_read_the_stats(tmp_path):
    verdict, code, score = rf.gate_verdict(tmp_path, rf.clean_env())  # no stats file at all
    assert (verdict, score) == ("error", None)
    assert code not in (0, 1)


def test_count_mutants_folds_suspicious_and_segfault_into_timeouts():
    run = rf.MutationRun(["s"])
    stats = {"killed": 5, "survived": 3, "no_tests": 2, "timeout": 1, "suspicious": 2,
             "segfault": 3, "skipped": 9}  # fmt: skip
    rf.count_mutants(run, stats)
    assert (run.killed, run.timeout, run.survived, run.no_tests) == (5, 6, 3, 2)
    assert run.mutants == 16


@pytest.mark.parametrize(
    "missing", ["killed", "survived", "no_tests", "timeout", "suspicious", "segfault"]
)
def test_count_mutants_fails_loudly_on_incomplete_stats(missing):
    stats = {
        "killed": 1,
        "survived": 1,
        "no_tests": 1,
        "timeout": 1,
        "suspicious": 0,
        "segfault": 0,
    }
    del stats[missing]
    with pytest.raises(KeyError, match=missing):
        rf.count_mutants(rf.MutationRun(["s"]), stats)


def test_a_fresh_mutation_run_is_an_error_until_the_gate_has_spoken():
    run = rf.MutationRun(["sel"])
    assert (run.gate, run.gate_exit, run.score) == ("error", -1, None)
    assert (run.gate_without_docstrings, run.score_without_docstrings) == ("error", None)
    assert (run.mutants, run.survivors, run.selectors) == (0, [], ["sel"])


def test_run_mutation_runs_mutmut_exports_the_stats_and_scores_them(tools, ws, tree, monkeypatch):
    clock = types.SimpleNamespace(now=0.0)

    def monotonic():
        clock.now += 2.5
        return clock.now

    monkeypatch.setattr(rf, "time", types.SimpleNamespace(monotonic=monotonic))
    run = rf.run_mutation(ws, bug(), tree, ["pkg.mod.x_check__mutmut_*"])
    assert run == rf.MutationRun(
        selectors=["pkg.mod.x_check__mutmut_*"],
        killed=3, timeout=0, survived=1, no_tests=0, mutants=4,
        score=75.0, gate="block", gate_exit=1, seconds=2.5,
    )  # fmt: skip
    assert (tree / "mutmut-run.log").read_text() == "running mutants\na warning\n"
    assert "only_mutate = pkg/mod.py" in (tree / "setup.cfg").read_text()
    assert tools.named("mutmut") == [
        [ws.tool("mutmut"), "run", "pkg.mod.x_check__mutmut_*"],
        [ws.tool("mutmut"), "export-cicd-stats"],
    ]


def test_run_mutation_tolerates_a_failing_mutmut_run_but_not_a_failing_export(tools, ws, tree):
    tools.codes["mutmut run"] = 1  # mutmut exits non-zero for plenty of benign reasons
    assert rf.run_mutation(ws, bug(), tree, ["s"]).mutants == 4
    tools.codes["mutmut export-cicd-stats"] = 1
    with pytest.raises(RuntimeError, match=r"^mutmut export-cicd-stats failed \(exit 1\)"):
        rf.run_mutation(ws, bug(), tree, ["s"])


def test_run_mutation_without_verdicts_is_an_error_not_a_zero_score(tools, ws, tree):
    tools.stats["p"] = dict.fromkeys(P_STATS, 0)
    with pytest.raises(RuntimeError, match=r"^mutmut gave no verdicts: running mutants"):
        rf.run_mutation(ws, bug(), tree, ["s"])
    assert not tools.named("python")  # the gate was never consulted
    assert all(c[1:3] != ["-m", "golden"] for c, _ in tools.calls)


def test_run_mutation_without_verdicts_quotes_the_last_600_characters_of_the_output(
    tools, ws, tree
):
    tools.run_out = "o" * 1000 + "TAIL"
    tools.stats["p"] = dict.fromkeys(P_STATS, 0)
    with pytest.raises(RuntimeError) as raised:
        rf.run_mutation(ws, bug(), tree, ["s"])
    assert str(raised.value) == "mutmut gave no verdicts: " + "o" * 596 + "TAIL"


def test_mutmut_runs_the_projects_tool_in_the_tree_with_the_projects_environment(
    monkeypatch, ws, tree
):
    calls = []
    monkeypatch.setattr(rf, "sh", lambda cmd, cwd, env=None: calls.append((cmd, cwd, env)))
    rf.mutmut(ws, tree, "show", "name")
    assert calls == [([ws.tool("mutmut"), "show", "name"], tree, ws.env())]


def test_run_mutation_fails_loudly_on_unreadable_stats(tools, ws, tree):
    tools.stats["p"] = {"killed": 3}
    with pytest.raises(KeyError, match="survived"):
        rf.run_mutation(ws, bug(), tree, ["s"])
    (tree / "mutants").mkdir(exist_ok=True)

    def garbage(cmd, cwd, env=None):
        (tree / "mutants" / "mutmut-cicd-stats.json").write_text("{not json", encoding="utf-8")
        return done(cmd)

    tools.codes.clear()
    original = tools._mutmut
    tools._mutmut = lambda cmd, cwd: (
        garbage(cmd, cwd) if cmd[1] == "export-cicd-stats" else original(cmd, cwd)
    )
    with pytest.raises(json.JSONDecodeError):
        rf.run_mutation(ws, bug(), tree, ["s"])


def test_run_mutation_fails_loudly_when_no_stats_file_appears(monkeypatch, ws, tree):
    monkeypatch.setattr(rf, "sh", lambda cmd, cwd, env=None: done(cmd))
    with pytest.raises(FileNotFoundError):
        rf.run_mutation(ws, bug(), tree, ["s"])


def test_run_mutation_scores_with_the_real_gate_identically(tools, ws, tree):
    tools.real_gate = True
    run = rf.run_mutation(ws, bug(), tree, ["s"])
    assert (run.score, run.gate, run.gate_exit) == (75.0, "block", 1)
    assert tools.gate["p"][0] == run.gate_exit  # what the script claims is what the CLI says


def test_list_survivors_keeps_survived_and_untested_mutants_of_the_selected_functions(
    tools, ws, tree
):
    tools.listing["p"] = (
        "Legend\n"
        "    pkg.mod.x_check__mutmut_1: killed\n"
        "    pkg.mod.x_check__mutmut_2: survived\n"
        "    pkg.mod.x_check__mutmut_3: timeout\n"
        "    pkg.mod.x_check__mutmut_4: no tests\n"
        "    pkg.mod.x_other__mutmut_1: survived\n"
        "    pkg.mod.xǁBoxǁmake__mutmut_7: survived\n"
        "  pkg.mod.x_check__mutmut_5: not checked\n"
        "pkg.mod.x_check__mutmut_6: survived\n"  # not indented: not a result line
    )
    got = rf.list_survivors(ws, tree, ["pkg.mod.x_check__mutmut_*", "pkg.mod.xǁBoxǁmake__*"])
    assert got == [
        rf.Survivor("pkg.mod.x_check__mutmut_2", "survived"),
        rf.Survivor("pkg.mod.x_check__mutmut_4", "no tests"),
        rf.Survivor("pkg.mod.xǁBoxǁmake__mutmut_7", "survived"),
    ]
    assert tools.named("mutmut", "results")[0][2:] == ["--all", "true"]


def test_list_survivors_fails_when_mutmut_fails(tools, ws, tree):
    tools.codes["mutmut results"] = 1
    with pytest.raises(RuntimeError, match=r"^mutmut results failed"):
        rf.list_survivors(ws, tree, ["*"])


MUTANT_NAMES = [
    ("pkg.mod.x_check__mutmut_3", "check"),
    ("pkg.mod.x__private__mutmut_12", "_private"),
    ("boltons.statsutils.xǁStatsǁ_calc_pearson_type__mutmut_53", "Stats._calc_pearson_type"),
    ("a.b.xǁKǁ__le____mutmut_1", "K.__le__"),
]


@pytest.mark.parametrize(("name", "key"), MUTANT_NAMES)
def test_mutant_names_split_into_class_and_function(name, key):
    m = rf.MUTANT.search(name)
    assert (f"{m['cls']}.{m['fn']}" if m["cls"] else m["fn"]) == key


@pytest.mark.parametrize("name", ["garbage", "pkg.mod.check__mutmut_1", "pkg.mod.x_check_mutmut_1"])
def test_mutant_pattern_rejects_other_names(name):
    assert rf.MUTANT.search(name) is None


# ------------------------------------------------------------------------ survivors
CODE_MUTANT = (
    "--- pkg/mod.py\n+++ pkg/mod.py\n@@ -1,5 +1,5 @@\n"
    "     if v > hi:\n-        return hi - 1\n+        return hi - 2\n     return v\n"
)
DOC_MUTANT = (
    "@@ -1,3 +1,3 @@\n def check(v, lo, hi):\n"
    '-    """Clamp v into [lo, hi]."""\n'
    '+    """XXClamp v into [lo, hi].XX"""\n'
    "     # bounds first\n"
)
FAULT = rf.Fault("modified", [10], [10], ["check"])


def survivor(name="pkg.mod.x_check__mutmut_1", diff=CODE_MUTANT, status="survived"):
    return rf.Survivor(name, status, diff=diff)


def test_locate_places_a_survivor_by_its_text_and_flags_the_fault():
    s = survivor()
    rf.locate(s, CHECK, FAULT)
    assert (s.function, s.lines) == ("check", [10])
    assert (s.on_fault_line, s.on_fault_statement, s.in_docstring) == (True, True, False)


def test_locate_flags_a_docstring_mutant_and_keeps_it_off_the_fault():
    s = survivor(diff=DOC_MUTANT)
    rf.locate(s, CHECK, FAULT)
    assert (s.function, s.lines) == ("check", [5])
    assert (s.on_fault_line, s.on_fault_statement, s.in_docstring) == (False, False, True)


def test_locate_a_mutant_on_any_line_of_a_multi_line_docstring_is_a_docstring_mutant():
    diff = (
        '@@ -1,4 +1,4 @@\n def top(a, b):\n-    """Doc one.\n+    """XXDoc one.\n'
        " \n     More doc.\n"
    )
    s = survivor("pkg.mod.x_top__mutmut_1", diff)
    rf.locate(s, STRUCT, None)
    assert (s.function, s.lines, s.in_docstring) == ("top", [7], True)  # docstring: lines 7-10


def test_locate_a_mutant_touching_docstring_and_code_is_not_a_docstring_mutant():
    s = survivor(diff=DOC_MUTANT + CODE_MUTANT.split("\n", 2)[2])
    rf.locate(s, CHECK, FAULT)
    assert s.lines == [5, 10]
    assert (s.in_docstring, s.on_fault_line) == (False, True)


def test_locate_without_a_fault_marks_nothing_as_faulty():
    s = survivor()
    rf.locate(s, CHECK, None)
    assert (s.lines, s.on_fault_line, s.on_fault_statement) == ([10], False, False)


def test_locate_distinguishes_a_fault_statement_from_a_fault_line():
    multi_fault = rf.Fault("modified", [3], [2, 3, 4, 5], ["build"])
    diff = "@@ -1,4 +1,4 @@\n     out = make(\n         a,\n-        b,\n+        c,\n     )\n"
    s = survivor("pkg.mod.x_build__mutmut_1", diff)
    rf.locate(s, MULTI, multi_fault)
    assert (s.lines, s.on_fault_line, s.on_fault_statement) == ([4], False, True)


def test_locate_handles_methods_and_decorated_functions():
    diff = (
        "@@ -1,3 +1,3 @@\n     total = 0\n"
        "-    for i in range(n):\n+    for i in range(n + 1):\n"
        "         total += i\n"
    )
    s = survivor("pkg.mod.xǁBoxǁmake__mutmut_4", diff)
    rf.locate(s, STRUCT, None)
    assert (s.function, s.lines) == ("Box.make", [23])


@pytest.mark.parametrize(
    "name", ["garbage", "pkg.mod.x_missing__mutmut_1", "pkg.mod.xǁBoxǁnope__mutmut_1"]
)
def test_locate_leaves_a_survivor_of_an_unknown_function_alone(name):
    s = survivor(name)
    rf.locate(s, CHECK, FAULT)
    assert (s.function, s.lines, s.on_fault_line, s.in_docstring) == ("", [], False, False)


def test_locate_leaves_lines_empty_when_the_hunk_text_is_not_in_the_source():
    s = survivor(diff="@@ -1 +1 @@\n-    something else entirely\n+    x\n")
    rf.locate(s, CHECK, FAULT)
    assert (s.function, s.lines, s.in_docstring) == ("check", [], False)


FIXED_FUNCTION = FIXED_CHECK.splitlines()[3:11]


def test_equals_fix_when_one_mutation_gives_exactly_the_fixed_function():
    exact = survivor(diff=MUTANT_2_DIFF)
    exact.function = "check"
    assert rf.equals_fix(exact, CHECK, FIXED_FUNCTION) is True
    other = survivor(diff=CODE_MUTANT)
    other.function = "check"
    assert rf.equals_fix(other, CHECK, FIXED_FUNCTION) is False


def test_equals_fix_ignores_comments_blank_lines_and_layout():
    fixed = [*FIXED_FUNCTION, "", "    # a comment the fix added"]
    fixed[6] = "        return   hi   # trailing"
    exact = survivor(diff=MUTANT_2_DIFF)
    exact.function = "check"
    assert rf.equals_fix(exact, CHECK, fixed) is True


def test_equals_fix_compares_the_function_alone_not_the_line_above_it():
    tight = CHECK.replace("import math\n\n\n", "import math\n")
    fixed = tight.replace("hi - 1", "hi").splitlines()[1:9]  # `def check` is line 2 here
    exact = survivor(diff=MUTANT_2_DIFF)
    exact.function = "check"
    assert rf.equals_fix(exact, tight, fixed) is True


def test_equals_fix_when_the_fix_adds_a_line():
    grown = CHECK.replace("    return v\n", "    return v\n    # end\n").replace(
        "        return hi - 1\n", "        hi -= 1\n        return hi\n"
    )
    diff = (
        "@@ -1,3 +1,4 @@\n     if v > hi:\n-        return hi - 1\n"
        "+        hi -= 1\n+        return hi\n     return v\n"
    )
    s = survivor(diff=diff)
    s.function = "check"
    fixed = grown.splitlines()[3:13]
    assert rf.equals_fix(s, CHECK, fixed) is True
    assert rf.equals_fix(s, CHECK, [*fixed[:-1], "    return None"]) is False


def test_equals_fix_is_false_for_unplaceable_or_unknown_survivors():
    s = survivor(diff="@@ -1 +1 @@\n-    not in the file\n+    x\n")
    s.function = "check"
    assert rf.equals_fix(s, CHECK, FIXED_FUNCTION) is False
    s = survivor(diff=MUTANT_2_DIFF)
    assert rf.equals_fix(s, CHECK, FIXED_FUNCTION) is False  # function not located
    s.function = "missing"
    assert rf.equals_fix(s, CHECK, FIXED_FUNCTION) is False
    s.function = "check"
    assert rf.equals_fix(s, CHECK, []) is False


def test_added_text_joins_the_stripped_added_lines_of_every_hunk():
    diff = "@@ -1 +1,2 @@\n-x\n+   a = 1  \n+b\n@@ -9 +10 @@\n context\n+  c\n"
    assert rf.added_text(diff) == "a = 1 b c"
    assert rf.added_text("@@ -1 +0,0 @@\n-gone\n") == ""
    assert rf.added_text("") == ""


@pytest.mark.parametrize(
    ("added", "fix_added", "ratio"),
    [
        ("return hi", "return hi", 1.0),
        ("ab", "abcd", 0.667),
        ("abc", "xyz", 0.0),
        ("", "return hi", 0.0),  # a deletion resembles nothing the fix adds
        ("", "", 0.0),  # ... not even a fix that adds nothing
    ],
)
def test_similarity(added, fix_added, ratio):
    diff = f"@@ -1 +1 @@\n-x\n+{added}\n" if added else "@@ -1 +0,0 @@\n-x\n"
    assert rf.similarity(survivor(diff=diff), fix_added) == ratio


def test_rank_survivors_sets_equality_with_the_fix_and_similarity():
    run = rf.MutationRun(["s"])
    exact = survivor("pkg.mod.x_check__mutmut_2", MUTANT_2_DIFF)
    off = survivor("pkg.mod.x_check__mutmut_3", CODE_MUTANT)
    unknown = survivor("pkg.mod.x_missing__mutmut_1", MUTANT_2_DIFF)
    elsewhere = survivor(
        "pkg.mod.x_other__mutmut_1",
        "@@ -1,2 +1,2 @@\n def other():\n-    return 0\n+    return 9\n",
    )
    for s in (exact, off, unknown, elsewhere):
        rf.locate(s, CHECK, FAULT)
    run.survivors = [exact, off, unknown, elsewhere]
    fix_diff = udiff(CHECK, FIXED_CHECK)
    # the fix did not touch `other`: its mutant has no fixed text to equal
    rf.rank_survivors(run, CHECK, {"check": FIXED_FUNCTION}, fix_diff)
    assert elsewhere.function == "other"
    # similarity is 2 * matching characters / total: "return hi - 2" (13) and "return 9" (8)
    # against the fix's "return hi" (9) share 9 and 7 characters
    assert [s.fix_equivalent for s in run.survivors] == [True, False, False, False]
    assert [s.similarity for s in run.survivors] == [1.0, 0.818, 1.0, 0.824]
    assert unknown.function == ""  # not located, so never equal to the fix


def ranked(name, *, line=False, statement=False, doc=False, equal=False, similarity=0.0):
    return rf.Survivor(
        name, "survived", on_fault_line=line, on_fault_statement=statement, in_docstring=doc,
        fix_equivalent=equal, similarity=similarity,
    )  # fmt: skip


def with_survivors(*survivors):
    run = rf.MutationRun(["s"])
    run.survivors = list(survivors)
    return run


def test_best_survivor_prefers_the_fix_then_the_closest_look_alike():
    a = ranked("a", line=True, similarity=0.2)
    b = ranked("b", line=True, similarity=0.9)
    c = ranked("c", line=True, equal=True, similarity=0.1)
    assert rf.best_survivor(with_survivors(a, b)) is b
    assert rf.best_survivor(with_survivors(a, b, c)) is c
    assert rf.best_survivor(with_survivors(a, ranked("t", line=True, similarity=0.2))) is a


def test_best_survivor_uses_statements_only_when_no_survivor_is_on_a_line():
    on_line = ranked("line", line=True, similarity=0.1)
    on_statement = ranked("stmt", statement=True, similarity=0.99, equal=True)
    assert rf.best_survivor(with_survivors(on_line, on_statement)) is on_line
    assert rf.best_survivor(with_survivors(on_statement)) is on_statement


def test_best_survivor_never_picks_a_docstring_mutant_or_an_unrelated_one():
    doc = ranked("doc", line=True, statement=True, doc=True, equal=True, similarity=1.0)
    stray = ranked("stray", similarity=1.0)
    assert rf.best_survivor(with_survivors(doc, stray)) is None
    assert rf.best_survivor(with_survivors()) is None
    fallback = ranked("fallback", statement=True)
    assert rf.best_survivor(with_survivors(doc, stray, fallback)) is fallback


def run_with(*, killed, survived=3, no_tests=1):
    return rf.MutationRun(
        ["pkg.mod.x_check__mutmut_*"], killed=killed, survived=survived, no_tests=no_tests
    )


DOC_ONLY_LISTING = (
    "    pkg.mod.x_check__mutmut_2: survived\n"
    "    pkg.mod.x_check__mutmut_3: survived\n"
    "    pkg.mod.x_check__mutmut_4: survived\n"
    "    pkg.mod.x_check__mutmut_5: no tests\n"
)


@pytest.mark.parametrize(("killed", "score", "gate"), [(4, 80.0, "pass"), (3, 75.0, "block")])
def test_describe_survivors_locates_each_one_and_rescores_without_docstring_mutants(
    tools, ws, tree, killed, score, gate
):
    tools.listing["p"] = DOC_ONLY_LISTING
    tools.shown.update(
        {
            "pkg.mod.x_check__mutmut_2": "header\n" + CODE_MUTANT,
            "pkg.mod.x_check__mutmut_3": "header\n" + DOC_MUTANT,
            "pkg.mod.x_check__mutmut_4": "header\n\n" + DOC_MUTANT + "\n",
            "pkg.mod.x_check__mutmut_5": "header\n" + DOC_MUTANT,
        }
    )
    run = run_with(killed=killed)
    text = (tree / "pkg" / "mod.py").read_text()
    rf.describe_survivors(ws, tree, run, text, FAULT)
    assert [(s.name[-1], s.status, s.lines, s.in_docstring) for s in run.survivors] == [
        ("2", "survived", [10], False),
        ("3", "survived", [5], True),
        ("4", "survived", [5], True),
        ("5", "no tests", [5], True),
    ]
    assert [s.on_fault_line for s in run.survivors] == [True, False, False, False]
    assert run.survivors[0].diff == CODE_MUTANT.rstrip("\n")  # header line dropped, trimmed
    assert run.survivors[2].diff == DOC_MUTANT.rstrip("\n")
    assert run.survived_docstring == 2
    assert run.score_without_docstrings == score
    assert run.gate_without_docstrings == gate
    assert sorted(c[2] for c in tools.named("mutmut", "show")) == [
        f"pkg.mod.x_check__mutmut_{n}" for n in (2, 3, 4, 5)
    ]


def test_describe_survivors_without_survivors_scores_the_counts_alone(tools, ws, tree):
    tools.listing["p"] = "    pkg.mod.x_check__mutmut_1: killed\n"
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=5, survived=0, no_tests=0)
    rf.describe_survivors(ws, tree, run, CHECK, None)
    assert run.survivors == []
    assert (run.survived_docstring, run.score_without_docstrings) == (0, 100.0)
    assert run.gate_without_docstrings == "pass"
    assert tools.named("mutmut", "show") == []


def test_describe_survivors_with_no_mutants_at_all_has_no_score_and_passes(tools, ws, tree):
    tools.listing["p"] = ""
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"])
    rf.describe_survivors(ws, tree, run, CHECK, None)
    assert (run.score_without_docstrings, run.gate_without_docstrings) == (None, "pass")


def test_describe_survivors_blocks_when_most_mutants_timed_out(tools, ws, tree):
    tools.listing["p"] = "    pkg.mod.x_check__mutmut_2: survived\n"
    tools.shown["pkg.mod.x_check__mutmut_2"] = "h\n" + CODE_MUTANT
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=1, timeout=9, survived=1)
    rf.describe_survivors(ws, tree, run, CHECK, FAULT)
    assert run.score_without_docstrings == 90.91  # 10 of 11, but 9 of the 11 timed out
    assert run.gate_without_docstrings == "block"


@pytest.mark.parametrize(
    "listing",
    [
        "",  # a listing nobody can read
        "Traceback (most recent call last):\n  File x\nboom\n",
        "    pkg.mod.x_check__mutmut_2: SURVIVED\n",
        "pkg.mod.x_check__mutmut_2 survived\n",
    ],
)
def test_describe_survivors_fails_when_the_listing_disagrees_with_the_stats(
    tools, ws, tree, listing
):
    tools.listing["p"] = listing
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1)
    with pytest.raises(RuntimeError, match=r"lists 0 survivors, its stats count 1"):
        rf.describe_survivors(ws, tree, run, CHECK, FAULT)
    assert tools.named("mutmut", "show") == []


def test_describe_survivors_fails_when_the_listing_has_more_than_the_stats(tools, ws, tree):
    tools.listing["p"] = LISTING_P.replace("x_other", "x_check")
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1)
    with pytest.raises(RuntimeError, match=r"lists 2 survivors, its stats count 1"):
        rf.describe_survivors(ws, tree, run, CHECK, FAULT)


@pytest.mark.parametrize(
    "shown", ["", "header only\n", "Traceback: boom\nValueError\n", "header\nno hunk here\n"]
)
def test_describe_survivors_fails_when_mutmut_show_prints_no_diff(tools, ws, tree, shown):
    tools.shown["pkg.mod.x_check__mutmut_2"] = shown
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1)
    with pytest.raises(
        RuntimeError, match=r"^mutmut show pkg.mod.x_check__mutmut_2 printed no diff"
    ):
        rf.describe_survivors(ws, tree, run, CHECK, FAULT)


def test_describe_survivors_quotes_the_last_300_characters_of_a_diffless_show(tools, ws, tree):
    tools.shown["pkg.mod.x_check__mutmut_2"] = "header\n" + "z" * 400 + "TAIL"
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1)
    with pytest.raises(RuntimeError) as raised:
        rf.describe_survivors(ws, tree, run, CHECK, FAULT)
    assert str(raised.value) == (
        "mutmut show pkg.mod.x_check__mutmut_2 printed no diff: " + "z" * 296 + "TAIL"
    )


@pytest.mark.parametrize(
    ("shown", "diff"),
    [
        ("header\n\n\n@@ -1 +1 @@\n-    a\n+    bX\n \n\n\n", "@@ -1 +1 @@\n-    a\n+    bX\n "),
        ("header\n@@ -1 +1 @@\n-    a\n+    bX\n", "@@ -1 +1 @@\n-    a\n+    bX"),
        ("header\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n", "@@ -1 +1 @@\n-a\n+b"),
    ],
)
def test_describe_survivors_keeps_the_diff_text_and_trims_only_newlines(
    tools, ws, tree, shown, diff
):
    tools.listing["p"] = "    pkg.mod.x_check__mutmut_2: survived\n"
    tools.shown["pkg.mod.x_check__mutmut_2"] = shown
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1)
    rf.describe_survivors(ws, tree, run, CHECK, FAULT)
    assert run.survivors[0].diff == diff


def test_describe_survivors_counts_an_untested_mutant_outside_docstrings(tools, ws, tree):
    tools.listing["p"] = (
        "    pkg.mod.x_check__mutmut_3: survived\n    pkg.mod.x_check__mutmut_6: no tests\n"
    )
    tools.shown["pkg.mod.x_check__mutmut_3"] = "h\n" + DOC_MUTANT
    tools.shown["pkg.mod.x_check__mutmut_6"] = "h\n" + CODE_MUTANT
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=4, survived=1, no_tests=1)
    rf.describe_survivors(ws, tree, run, CHECK, FAULT)
    assert run.survived_docstring == 1
    # 4 killed of 5 valid: only the docstring survivor is dropped, the untested code mutant counts
    assert run.score_without_docstrings == 80.0
    assert run.gate_without_docstrings == "pass"
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1, no_tests=1)
    rf.describe_survivors(ws, tree, run, CHECK, FAULT)
    assert run.score_without_docstrings == 75.0
    assert run.gate_without_docstrings == "block"


def test_describe_survivors_fails_when_mutmut_show_fails(tools, ws, tree):
    tools.codes["mutmut show"] = 1
    run = rf.MutationRun(["pkg.mod.x_check__mutmut_*"], killed=3, survived=1)
    with pytest.raises(RuntimeError, match=r"^mutmut show pkg.mod.x_check__mutmut_2 failed"):
        rf.describe_survivors(ws, tree, run, CHECK, FAULT)


# ----------------------------------------------------------------------------- gates
def cov(gate):
    return {"diff_coverage_gate": gate}


def with_gate(gate):
    run = rf.MutationRun(["s"])
    run.gate = gate
    return run


@pytest.mark.parametrize(
    ("coverage_gate", "mutation_gate", "coverage_only", "full"),
    [
        ("block", "pass", "flags", "flags"),
        ("block", "block", "flags", "flags"),
        ("pass", "block", "misses", "flags"),
        ("pass", "pass", "misses", "misses"),
        ("pass", "error", "misses", "misses"),
    ],
)
def test_gate_decisions(coverage_gate, mutation_gate, coverage_only, full):
    assert rf.gate_decisions(cov(coverage_gate), with_gate(mutation_gate)) == {
        "test_presence": "not evaluated",
        "diff_coverage": coverage_gate,
        "mutation": mutation_gate,
        "coverage_only_pipeline": coverage_only,
        "full_pipeline": full,
    }


# ----------------------------------------------------------------------------- reporting
@pytest.mark.parametrize(
    ("n", "d", "text"),
    [(3, 10, "3/10 (30%)"), (0, 10, "0/10 (0%)"), (10, 10, "10/10 (100%)"), (1, 3, "1/3 (33%)"),
     (2, 3, "2/3 (67%)"), (0, 0, "n/a"), (5, 0, "n/a")],
)  # fmt: skip
def test_pct(n, d, text):
    assert rf.pct(n, d) == text


@pytest.mark.parametrize(
    ("values", "answer"),
    [
        ([], None),
        ([84.9], 84.9),
        ([3.0, 1.0, 2.0], 2.0),
        ([70.77, 38.96, 60.0, 0.0], 49.5),  # mean of 38.96 and 60.0, rounded to one decimal
        ([1, 2], 1.5),
    ],
)
def test_median(values, answer):
    assert rf.median(values) == answer


@pytest.mark.parametrize(
    ("score", "gate", "text"),
    [(None, "pass", "n/a"), (84.9, "pass", "84.9% PASS"), (80.0, "block", "80% BLOCK"),
     (38.96, "block", "38.96% BLOCK"), (0.0, "block", "0% BLOCK"), (100.0, "pass", "100% PASS")],
)  # fmt: skip
def test_cell(score, gate, text):
    assert rf.cell(score, gate) == text


def mrun(score, gate, nodoc, survivors=()):
    return {"score": score, "gate": gate, "gate_without_docstrings": nodoc,
            "survivors": [dict(s) for s in survivors]}  # fmt: skip


def rec(ident, *, blind, cov_gate, p, f, only, full):
    return {
        "id": ident,
        "coverage": {"all_executed": blind, "diff_coverage_gate": cov_gate},
        "mutation_parent": p,
        "mutation_fix": f,
        "gates": {"coverage_only_pipeline": only, "full_pipeline": full},
    }


def live(**flags):
    return {"on_fault_line": False, "in_docstring": False, "fix_equivalent": False, **flags}


HAND_RESULTS = [
    rec("one", blind=True, cov_gate="pass", only="misses", full="misses",
        p=mrun(90.0, "pass", "pass", [live(on_fault_line=True)]), f=mrun(95.0, "pass", "pass")),
    rec("two", blind=False, cov_gate="block", only="flags", full="flags",
        p=mrun(50.0, "block", "block", [live(on_fault_line=True, fix_equivalent=True)]),
        f=mrun(50.0, "block", "block")),
    rec("three", blind=True, cov_gate="pass", only="misses", full="flags",
        p=mrun(70.0, "block", "pass", [live(on_fault_line=True, in_docstring=True)]),
        f=mrun(60.0, "block", "block")),
    {"id": "four", "project": "p", "skipped": "slow"},
    {"id": "five", "project": "p", "error": "boom"},
]  # fmt: skip


def test_headline_counts_each_metric_from_the_records_alone():
    assert rf.headline(HAND_RESULTS) == {
        "bugs_evaluated": 3,
        "bugs_errored": 1,
        "bugs_skipped": 1,
        "faulty_lines_all_executed_at_P": 2,
        "diff_coverage_gate_passes_at_P": 2,
        "mutation_gate_blocks_at_P": 2,
        "mutation_gate_blocks_at_P_without_docstring_mutants": 1,
        "survivor_on_faulty_line_at_P": 3,
        "survivor_on_faulty_line_at_P_excluding_docstrings": 2,
        "fix_is_a_surviving_mutant_at_P": 1,
        "score_raised_at_F": 1,
        "score_equal_at_F": 1,
        "score_lower_at_F": 1,
        "gate_blocks_at_F": 2,
        "coverage_blind_and_gate_blocks": 1,
        "coverage_blind_and_survivor_on_fault": 2,
        "coverage_blind_and_gate_passes": 1,
        "coverage_only_pipeline_flags": 1,
        "full_pipeline_flags": 2,
        "median_score_at_P": 70.0,
        "median_score_at_F": 60.0,
    }


def test_headline_of_nothing_is_all_zeros_and_no_medians():
    h = rf.headline([])
    assert h["bugs_evaluated"] == 0
    assert [v for k, v in h.items() if not k.startswith("median")] == [0] * 19
    assert (h["median_score_at_P"], h["median_score_at_F"]) == (None, None)
    unlucky = rf.headline([{"id": "a", "skipped": "x"}, {"id": "b", "error": "y"}])
    assert (unlucky["bugs_evaluated"], unlucky["bugs_skipped"], unlucky["bugs_errored"]) == (
        0,
        1,
        1,
    )
    assert unlucky["median_score_at_P"] is None


def test_headline_rows_name_real_headline_keys():
    keys = {key for _, key in rf.HEADLINE_ROWS}
    assert keys <= set(rf.headline([]))
    assert len(keys) == len(rf.HEADLINE_ROWS)


def test_render_headline_lists_every_row_with_its_share():
    h = rf.headline(HAND_RESULTS)
    text = rf.render_headline(h)
    lines = text.splitlines()
    assert text.endswith("\n")
    assert lines[0] == "- bugs evaluated: 3 (tool errors: 1)"
    assert lines[-1] == "- median score: 70.0 at P, 60.0 at F"
    assert len(lines) == 2 + len(rf.HEADLINE_ROWS)
    assert "- coverage-only pipeline (diff coverage) flags the faulty code: 1/3 (33%)" in lines
    assert "- full pipeline (diff coverage + mutation) flags it: 2/3 (67%)" in lines
    assert "- mutation gate blocks at P: 2/3 (67%)" in lines


def test_render_headline_of_nothing_is_n_a():
    lines = rf.render_headline(rf.headline([])).splitlines()
    assert lines[0] == "- bugs evaluated: 0 (tool errors: 0)"
    assert all(ln.endswith(": n/a") for ln in lines[1:-1])
    assert lines[-1] == "- median score: None at P, None at F"


def test_row_of_a_bug_that_ran():
    r = {
        "id": "proj-x",
        "fault": {"functions": ["a", "B.c"], "kind": "modified"},
        "coverage": {"all_executed": False, "percent": 50.0},
        "mutation_parent": {
            "score": 70.77, "gate": "block", "survived": 15, "no_tests": 4,
            "survivors": [{"on_fault_line": False, "fix_equivalent": True},
                          {"on_fault_line": True, "fix_equivalent": False}],
        },
        "mutation_fix": {"score": 80.0, "gate": "pass"},
        "gates": {"coverage_only_pipeline": "flags", "full_pipeline": "flags"},
    }  # fmt: skip
    assert rf.row(r) == (
        "| proj-x | a, B.c | modified | NO (50.0%) | 70.77% BLOCK (19 surv.) | yes | yes | "
        "80% PASS | flags | flags |"
    )
    r["coverage"] = {"all_executed": True, "percent": 100.0}
    r["mutation_parent"]["survivors"] = []
    r["mutation_parent"]["score"] = None
    assert rf.row(r) == (
        "| proj-x | a, B.c | modified | yes (100.0%) | n/a (19 surv.) | no | no | "
        "80% PASS | flags | flags |"
    )


def test_row_of_a_skipped_or_failed_bug_truncates_the_reason():
    assert rf.row({"id": "s", "skipped": "short"}) == "| s | | | | short... | | | | | |"
    long = "r" * 70
    assert rf.row({"id": "s", "skipped": long}) == f"| s | | | | {'r' * 60}... | | | | | |"
    assert rf.row({"id": "e", "error": "e" * 100}) == f"| e | | | | error: {'e' * 80} | | | | | |"


def test_table_has_a_header_and_one_row_per_bug():
    results = [{"id": "s", "skipped": "x"}, {"id": "e", "error": "y"}]
    text = rf.table(results)
    lines = text.splitlines()
    assert text.endswith("\n")
    assert lines[0].startswith("| Bug | Function(s) | Fault | Faulty lines executed at P |")
    assert lines[0].count("|") == lines[1].count("|") == 11
    assert lines[1] == "|---|---|---|---|---|---|---|---|---|---|"
    assert lines[2:] == ["| s | | | | x... | | | | | |", "| e | | | | error: y | | | | | |"]


# ----------------------------------------------------------------------------- end to end
MOD_MANIFEST = """\
[[project]]
name = "proj"
url = "{url}"
python = "3.12"
source_paths = ["pkg/"]
test_paths = ["tests"]

[[bug]]
id = "proj-clamp-high"
project = "proj"
fix = "{fix}"
parent = "{parent}"
file = "pkg/mod.py"
functions = ["check"]
regression_tests = ["tests/test_mod.py::test_high"]

[[bug]]
id = "proj-slow"
project = "proj"
fix = "{fix}"
parent = "{parent}"
file = "pkg/mod.py"
functions = ["check"]
regression_tests = ["tests/test_mod.py::test_high"]
skip = "takes too long"
"""
BROKEN_BUG = """
[[bug]]
id = "proj-broken"
project = "proj"
fix = "{fix}"
parent = "{parent}"
file = "pkg/mod.py"
functions = ["not_there"]
regression_tests = ["tests/test_mod.py::test_high"]
"""


@pytest.fixture
def bench(tmp_path, make_repo, tools):
    """A local upstream with one bug fix (P -> F), a workspace over it and scripted tools."""
    up = make_repo(tmp_path / "upstream")
    untouched = "def untouched():\n    return 1\n"
    parent = up.commit(
        {"pkg/mod.py": CHECK, "pkg/idle.py": untouched, "tests/test_mod.py": TEST_P,
         "tests/data.txt": "v1\n"}, "buggy",
    )  # fmt: skip
    fix = up.commit(
        {"pkg/mod.py": FIXED_CHECK, "tests/test_mod.py": TEST_F, "tests/data.txt": "v2\n"}, "fix"
    )
    # upstream moved on: the clone's checkout is not F, so nothing may read the working tree
    later = {"pkg/mod.py": FIXED_CHECK + "\n# later\n", "tests/test_late.py": "x = 1\n"}
    up.commit(later, "later")
    p = project(url=str(up.root))
    b = bug(parent=parent, fix=fix)
    manifest = tmp_path / "manifest.toml"
    text = MOD_MANIFEST.format(url=up.root, fix=fix, parent=parent)
    manifest.write_text(text, encoding="utf-8")
    ws = rf.Workspace(tmp_path / "work", p)
    ws.root.mkdir()
    return types.SimpleNamespace(
        up=up,
        parent=parent,
        fix=fix,
        project=p,
        bug=b,
        ws=ws,
        manifest=manifest,
        tools=tools,
        text=text,
    )


def test_evaluate_bug_end_to_end_on_a_local_repository(bench):
    rf.prepare(bench.ws)
    assert bench.ws.clone.joinpath("pkg", "mod.py").is_file()  # a real clone of the upstream
    assert bench.ws.venv.is_dir()
    result = rf.evaluate_bug(bench.ws, bench.bug)
    assert result["id"] == "proj-clamp-high"
    assert (result["project"], result["file"]) == ("proj", "pkg/mod.py")
    assert result["verify"] == {
        "changed_test_files": ["tests/test_mod.py"],
        "failing_at_parent": ["tests/test_mod.py::test_high"],
        "failing_at_fix": [],
        "regression_fails_at_parent": True,
        "regression_passes_at_fix": True,
    }
    assert result["fault"] == {
        "kind": "modified", "lines": [10], "statement_lines": [10], "functions": ["check"],
        "fix_size": 2,
    }  # fmt: skip
    assert result["coverage"] == {
        "executable_faulty_lines": [10], "executed": [10], "not_executed": [],
        "partial_branch_lines": [], "percent": 100.0, "all_executed": True,
        "diff_coverage_gate": "pass",
    }  # fmt: skip
    assert result["selectors"] == ["pkg.mod.x_check__mutmut_*"]
    parent_run, fix_run = result["mutation_parent"], result["mutation_fix"]
    assert (parent_run["score"], parent_run["gate"], parent_run["gate_exit"]) == (75.0, "block", 1)
    assert (fix_run["score"], fix_run["gate"], fix_run["gate_exit"]) == (100.0, "pass", 0)
    assert parent_run["survivors"] == [
        {"name": "pkg.mod.x_check__mutmut_2", "status": "survived", "function": "check",
         "lines": [10], "on_fault_line": True, "on_fault_statement": True,
         "in_docstring": False, "fix_equivalent": True, "similarity": 1.0,
         "diff": MUTANT_2_DIFF.rstrip("\n")}
    ]  # fmt: skip
    assert fix_run["survivors"] == []
    assert result["best_survivor"] == parent_run["survivors"][0]
    assert result["gates"] == {
        "test_presence": "not evaluated", "diff_coverage": "pass", "mutation": "block",
        "coverage_only_pipeline": "misses", "full_pipeline": "flags",
    }  # fmt: skip
    assert set(result["seconds"]) == {"verify", "coverage", "mutation_parent", "mutation_fix"}
    # regression test runs saw F's tests on P and on F, in that order
    assert [seen[0] for seen in bench.tools.pytest_seen] == ["verify-p", "verify-f"]
    assert all(seen[1] == TEST_F for seen in bench.tools.pytest_seen)


def test_evaluate_bug_times_each_phase_in_tenths_of_a_second(bench, monkeypatch):
    rf.prepare(bench.ws)
    clock = types.SimpleNamespace(now=0.0)

    def monotonic():
        clock.now += 0.26
        return clock.now

    monkeypatch.setattr(rf, "time", types.SimpleNamespace(monotonic=monotonic))
    result = rf.evaluate_bug(bench.ws, bench.bug)
    # every reading is 0.26 s after the previous one; a phase lasts one reading (verify,
    # coverage) or three (the two mutation phases, which read the clock twice inside)
    assert result["seconds"] == {
        "verify": 0.3,
        "coverage": 0.3,
        "mutation_parent": 0.8,
        "mutation_fix": 0.8,
    }
    assert result["mutation_parent"]["seconds"] == 0.3
    assert result["mutation_fix"]["seconds"] == 0.3


def test_evaluate_bug_locates_survivors_of_the_fixed_function_too(bench):
    rf.prepare(bench.ws)
    diff = (
        "@@ -1,3 +1,3 @@\n     if v > hi:\n-        return hi\n"
        "+        return hi + 1\n     return v"
    )
    bench.tools.stats["f"] = {**F_STATS, "killed": 3, "survived": 1}
    bench.tools.listing["f"] = "    pkg.mod.x_check__mutmut_7: survived\n"
    bench.tools.shown["pkg.mod.x_check__mutmut_7"] = "# header\n" + diff
    result = rf.evaluate_bug(bench.ws, bench.bug)
    assert result["mutation_fix"]["survivors"] == [
        {
            "name": "pkg.mod.x_check__mutmut_7",
            "status": "survived",
            "function": "check",
            "lines": [10],
            "on_fault_line": False,  # there is no fault to be on at F
            "on_fault_statement": False,
            "in_docstring": False,
            "fix_equivalent": False,
            "similarity": 0.0,
            "diff": diff,
        }
    ]


def test_evaluate_bug_with_the_real_gate_gives_the_same_result(bench):
    rf.prepare(bench.ws)
    scripted = rf.evaluate_bug(bench.ws, bench.bug)
    bench.tools.real_gate = True
    real = rf.evaluate_bug(bench.ws, bench.bug)
    for result in (scripted, real):
        result.pop("seconds")
        result["mutation_parent"].pop("seconds")
        result["mutation_fix"].pop("seconds")
    assert real == scripted


def test_evaluate_bug_rejects_a_fix_that_changes_none_of_the_manifest_functions(bench):
    rf.prepare(bench.ws)
    idle = rf.Bug(**{**bench.bug.__dict__, "file": "pkg/idle.py", "functions": ("untouched",)})
    bench.tools.coverage["files"]["pkg/idle.py"] = {"executed_lines": [1, 2], "missing_lines": []}
    with pytest.raises(RuntimeError, match="do not match the manifest's functions"):
        rf.evaluate_bug(bench.ws, idle)
    assert bench.tools.named("mutmut") == []  # no mutation run on a diff nobody understood


def test_evaluate_bug_fails_loudly_on_a_function_that_is_not_in_the_source(bench):
    rf.prepare(bench.ws)
    with pytest.raises(KeyError, match="not_there"):
        rf.evaluate_bug(bench.ws, rf.Bug(**{**bench.bug.__dict__, "functions": ("not_there",)}))


def test_evaluate_bug_stops_at_a_parent_whose_suite_fails(bench):
    rf.prepare(bench.ws)
    bench.tools.codes["coverage run"] = 1
    with pytest.raises(RuntimeError, match="P's own suite does not pass here"):
        rf.evaluate_bug(bench.ws, bench.bug)
    assert bench.tools.named("mutmut") == []


def test_evaluate_bug_stops_when_mutmut_produces_nothing(bench):
    rf.prepare(bench.ws)
    bench.tools.stats["p"] = dict.fromkeys(P_STATS, 0)
    with pytest.raises(RuntimeError, match="mutmut gave no verdicts"):
        rf.evaluate_bug(bench.ws, bench.bug)


def test_evaluate_bug_stops_when_the_survivor_listing_is_unreadable(bench):
    rf.prepare(bench.ws)
    bench.tools.listing["p"] = "unexpected format\n"
    with pytest.raises(RuntimeError, match="lists 0 survivors, its stats count 1"):
        rf.evaluate_bug(bench.ws, bench.bug)


def test_evaluate_bug_has_no_best_survivor_when_none_is_on_the_fault(bench):
    rf.prepare(bench.ws)
    bench.tools.stats["p"] = {**P_STATS, "killed": 4, "survived": 0}
    bench.tools.listing["p"] = LISTING_F
    result = rf.evaluate_bug(bench.ws, bench.bug)
    assert result["best_survivor"] is None
    assert result["mutation_parent"]["survivors"] == []


# ----------------------------------------------------------------------------- orchestration
def args_for(manifest, *extra):
    return rf.parse_args(["--manifest", str(manifest), *extra])


@pytest.fixture
def stubbed(monkeypatch):
    """`prepare`, `evaluate_bug` and `verify` replaced by recorders, for run_all/main."""
    state = types.SimpleNamespace(prepared=[], evaluated=[], verified=[], raises={}, workspaces=[])

    def prepare(ws):
        state.prepared.append(ws.project.name)

    def evaluate(ws, b):
        state.workspaces.append(ws)
        state.evaluated.append(b.id)
        if b.id in state.raises:
            raise state.raises[b.id]
        return {"id": b.id, "project": b.project, "evaluated": True}

    def verify(ws, b):
        state.workspaces.append(ws)
        state.verified.append(b.id)
        return {"verified": b.id}

    monkeypatch.setattr(rf, "prepare", prepare)
    monkeypatch.setattr(rf, "evaluate_bug", evaluate)
    monkeypatch.setattr(rf, "verify", verify)
    return state


@pytest.fixture
def three_bugs(tmp_path):
    parts = [MOD_MANIFEST.format(url="u", fix="1" * 40, parent="2" * 40)]
    parts.append(BROKEN_BUG.format(fix="1" * 40, parent="2" * 40).replace("not_there", "other"))
    path = tmp_path / "manifest.toml"
    path.write_text("".join(parts), encoding="utf-8")
    return path


def test_parse_args_defaults():
    args = rf.parse_args([])
    assert args.manifest == RF_DIR / "manifest.toml"
    assert (args.only, args.workdir, args.out) == ([], None, None)
    assert (args.keep, args.verify_only) == (False, False)


def test_parse_args_reads_every_option(tmp_path):
    args = rf.parse_args(
        ["--manifest", "m.toml", "--only", "a", "--only", "b", "--workdir", str(tmp_path / "w"),
         "--out", str(tmp_path / "o"), "--keep", "--verify-only"]
    )  # fmt: skip
    assert args.manifest == Path("m.toml")
    assert args.only == ["a", "b"]
    assert (args.workdir, args.out) == (tmp_path / "w", tmp_path / "o")
    assert (args.keep, args.verify_only) == (True, True)


def test_parse_args_rejects_unknown_options_and_documents_itself(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as raised:
        rf.parse_args(["--no-such-option"])
    assert raised.value.code == 2
    assert "unrecognized arguments: --no-such-option" in capsys.readouterr().err
    with pytest.raises(SystemExit) as helped:
        rf.parse_args(["--help"])
    assert helped.value.code == 0
    out = capsys.readouterr().out
    assert "Real-fault benchmark" in out
    # the module docstring is shown as written (RawTextHelpFormatter)
    assert "    uv run python evals/realfaults/run.py                  # whole manifest" in out
    for text in (
        "--only ID",
        "bug id (repeatable)",
        "scratch directory (default: a temp dir)",
        "where results.json and results.md go",
        "keep the scratch directory",
        "only check regression tests fail on P, pass on F",
        "--manifest MANIFEST",
    ):
        assert text in out


def test_run_all_evaluates_in_manifest_order_and_records_skips(
    stubbed, three_bugs, tmp_path, capsys
):
    results = rf.run_all(args_for(three_bugs, "--workdir", str(tmp_path / "w")))
    assert results == [
        {"id": "proj-clamp-high", "project": "proj", "evaluated": True},
        {"id": "proj-slow", "project": "proj", "skipped": "takes too long"},
        {"id": "proj-broken", "project": "proj", "evaluated": True},
    ]
    assert stubbed.evaluated == ["proj-clamp-high", "proj-broken"]
    assert stubbed.prepared == ["proj", "proj"]  # never for the skipped bug
    assert capsys.readouterr().out == "== proj-clamp-high\n== proj-slow\n== proj-broken\n"
    assert (tmp_path / "w").is_dir()  # an explicit workdir is created and kept


def test_run_all_only_selects_bugs_and_rejects_unknown_ids(stubbed, three_bugs, tmp_path):
    work = str(tmp_path / "w")
    results = rf.run_all(args_for(three_bugs, "--workdir", work, "--only", "proj-broken"))
    assert [r["id"] for r in results] == ["proj-broken"]
    with pytest.raises(SystemExit, match=r"unknown bug ids: \['nope', 'zzz'\]"):
        rf.run_all(args_for(three_bugs, "--workdir", work, "--only", "zzz", "--only", "nope"))
    with pytest.raises(SystemExit, match=r"unknown bug ids: \['nope'\]"):
        rf.run_all(
            args_for(three_bugs, "--workdir", work, "--only", "proj-broken", "--only", "nope")
        )


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (RuntimeError("tool failed"), "tool failed"),
        (KeyError("pkg/mod.py"), "'pkg/mod.py'"),
        (SyntaxError("bad syntax"), "bad syntax"),
        (
            subprocess.TimeoutExpired("mutmut run", 5),
            "Command 'mutmut run' timed out after 5 seconds",
        ),
    ],
)
def test_run_all_records_a_tool_failure_and_carries_on(
    stubbed, three_bugs, tmp_path, capsys, error, text
):
    stubbed.raises["proj-clamp-high"] = error
    results = rf.run_all(args_for(three_bugs, "--workdir", str(tmp_path / "w")))
    assert results[0] == {"id": "proj-clamp-high", "project": "proj", "error": text}
    assert results[2]["evaluated"] is True  # the next bug still ran
    assert f"== proj-clamp-high\n   error: {text}\n== proj-slow\n" in capsys.readouterr().out


def test_run_all_records_a_failing_prepare_as_an_error(monkeypatch, stubbed, three_bugs, tmp_path):
    def prepare(ws):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(rf, "prepare", prepare)
    results = rf.run_all(args_for(three_bugs, "--workdir", str(tmp_path / "w")))
    assert results[0] == {"id": "proj-clamp-high", "project": "proj", "error": "clone failed"}
    assert stubbed.evaluated == []


@pytest.mark.parametrize("error", [ValueError("a bug in the harness"), TypeError("so is this")])
def test_run_all_lets_unexpected_exceptions_through(stubbed, three_bugs, tmp_path, error):
    stubbed.raises["proj-clamp-high"] = error
    with pytest.raises(type(error)):
        rf.run_all(args_for(three_bugs, "--workdir", str(tmp_path / "w")))


def test_run_all_with_verify_only_prints_the_verification_and_collects_nothing(
    stubbed, three_bugs, tmp_path, capsys
):
    results = rf.run_all(args_for(three_bugs, "--workdir", str(tmp_path / "w"), "--verify-only"))
    assert results == [{"id": "proj-slow", "project": "proj", "skipped": "takes too long"}]
    assert stubbed.verified == ["proj-clamp-high", "proj-broken"]
    assert stubbed.evaluated == []
    assert [(w.root, w.project.name) for w in stubbed.workspaces] == [(tmp_path / "w", "proj")] * 2
    out = capsys.readouterr().out
    assert json.dumps({"verified": "proj-clamp-high"}, indent=2) in out


def test_run_all_creates_the_workdir_and_its_missing_parents(stubbed, three_bugs, tmp_path):
    work = tmp_path / "a" / "b" / "w"
    rf.run_all(args_for(three_bugs, "--workdir", str(work)))
    assert work.is_dir()
    assert {w.root for w in stubbed.workspaces} == {work}
    rf.run_all(args_for(three_bugs, "--workdir", str(work)))  # an existing one is fine


def scratch(tmp_path, monkeypatch):
    path = tmp_path / "scratch"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def test_run_all_removes_its_temporary_workdir_unless_told_to_keep_it(
    stubbed, three_bugs, tmp_path, monkeypatch
):
    root = scratch(tmp_path, monkeypatch)
    rf.run_all(args_for(three_bugs))
    assert list(root.iterdir()) == []
    rf.run_all(args_for(three_bugs, "--keep"))
    kept = list(root.iterdir())
    assert len(kept) == 1
    assert kept[0].name.startswith("realfaults-")
    assert kept[0].is_dir()


def test_run_all_with_an_unreadable_manifest_runs_nothing(stubbed, tmp_path):
    path = tmp_path / "manifest.toml"
    path.write_text(MANIFEST.replace('project = "beta"', 'project = "ghost"'), encoding="utf-8")
    with pytest.raises(SystemExit, match="unknown projects"):
        rf.run_all(args_for(path))
    assert stubbed.prepared == []


def test_main_end_to_end_writes_results_and_a_markdown_report(bench, tmp_path, capsys):
    out = tmp_path / "out"
    code = rf.main(["--manifest", str(bench.manifest), "--workdir", str(tmp_path / "w2"),
                    "--out", str(out)])  # fmt: skip
    assert code == 0
    document = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert list(document) == ["headline", "bugs", "runtime_seconds"]
    raw = (out / "results.json").read_text(encoding="utf-8")
    assert raw == json.dumps(document, indent=2) + "\n"
    assert isinstance(document["runtime_seconds"], int)
    ran, skipped = document["bugs"]
    assert ran["id"] == "proj-clamp-high"
    assert ran["gates"]["full_pipeline"] == "flags"
    assert skipped == {"id": "proj-slow", "project": "proj", "skipped": "takes too long"}
    assert document["headline"] == rf.headline(document["bugs"])
    assert document["headline"]["bugs_evaluated"] == 1
    assert document["headline"]["full_pipeline_flags"] == 1
    markdown = (out / "results.md").read_text(encoding="utf-8")
    assert markdown == rf.table(document["bugs"]) + "\n" + rf.render_headline(document["headline"])
    shown = capsys.readouterr().out
    assert shown.startswith("== proj-clamp-high\n== proj-slow\n")
    assert shown.endswith(markdown + "\n")


def test_main_exits_nonzero_when_a_bug_errors_but_still_writes_everything(bench, tmp_path):
    bench.manifest.write_text(
        bench.text + BROKEN_BUG.format(fix=bench.fix, parent=bench.parent), encoding="utf-8"
    )
    out = tmp_path / "out"
    code = rf.main(["--manifest", str(bench.manifest), "--workdir", str(tmp_path / "w2"),
                    "--out", str(out)])  # fmt: skip
    assert code == 1
    document = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in document["bugs"]] == ["proj-clamp-high", "proj-slow", "proj-broken"]
    assert document["bugs"][2] == {"id": "proj-broken", "project": "proj", "error": "'not_there'"}
    assert (document["headline"]["bugs_evaluated"], document["headline"]["bugs_errored"]) == (1, 1)
    assert "error: 'not_there'" in (out / "results.md").read_text(encoding="utf-8")


def test_main_with_nothing_evaluated_writes_no_headline(stubbed, three_bugs, tmp_path, capsys):
    out = tmp_path / "out" / "nested"
    work = str(tmp_path / "w")
    argv = ["--manifest", str(three_bugs), "--out", str(out), "--workdir", work]
    code = rf.main([*argv, "--only", "proj-slow"])
    assert code == 0
    document = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert document["headline"] == {}
    assert document["bugs"] == [{"id": "proj-slow", "project": "proj", "skipped": "takes too long"}]
    markdown = (out / "results.md").read_text(encoding="utf-8")
    assert markdown == rf.table(document["bugs"])
    assert "bugs evaluated" not in markdown
    assert markdown in capsys.readouterr().out


def test_main_runtime_is_rounded_wall_clock_seconds(stubbed, three_bugs, tmp_path, monkeypatch):
    ticks = iter([10.0, 14.6])
    monkeypatch.setattr(rf, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    out = tmp_path / "out"
    rf.main(["--manifest", str(three_bugs), "--out", str(out), "--workdir", str(tmp_path / "w"),
             "--only", "proj-slow"])  # fmt: skip
    assert json.loads((out / "results.json").read_text(encoding="utf-8"))["runtime_seconds"] == 5


def test_a_partial_run_never_overwrites_the_committed_results(stubbed, tmp_path, monkeypatch):
    committed = {name: (RF_DIR / name).read_bytes() for name in ("results.json", "results.md")}
    root = scratch(tmp_path, monkeypatch)
    code = rf.main(["--only", "more-itertools-bucket-falsey-validator"])
    assert code == 0
    assert (root / "results.json").is_file()
    assert (root / "results.md").is_file()
    document = json.loads((root / "results.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in document["bugs"]] == ["more-itertools-bucket-falsey-validator"]
    assert "skipped" in document["bugs"][0]
    assert {name: (RF_DIR / name).read_bytes() for name in committed} == committed


def test_a_whole_run_writes_next_to_the_script_by_default(stubbed, tmp_path, monkeypatch):
    here = tmp_path / "evals" / "realfaults"
    here.mkdir(parents=True)
    manifest = MOD_MANIFEST.replace(
        'test_high"]\n\n[[bug]]', 'test_high"]\nskip = "also slow"\n\n[[bug]]'
    )  # no bug runs: the results table needs no evaluated records
    (here / "manifest.toml").write_text(manifest.format(url="u", fix="1" * 40, parent="2" * 40))
    monkeypatch.setattr(rf, "HERE", here)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "nowhere"))
    assert rf.main(["--workdir", str(tmp_path / "w")]) == 0
    document = json.loads((here / "results.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in document["bugs"]] == ["proj-clamp-high", "proj-slow"]
    assert (here / "results.md").is_file()
    assert not (tmp_path / "nowhere").exists()


def test_main_fails_loudly_on_a_bad_manifest_and_writes_nothing(tmp_path):
    path = tmp_path / "manifest.toml"
    path.write_text(MANIFEST.replace('fix = "1111111111111111111111111111111111111111"\n', ""))
    out = tmp_path / "out"
    with pytest.raises(TypeError, match="fix"):
        rf.main(["--manifest", str(path), "--out", str(out)])
    assert not out.exists()
