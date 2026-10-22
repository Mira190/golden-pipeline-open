"""docs/alignment.md stays true: every piece of evidence it cites must exist.

The alignment matrix maps published engineering practice to this repository's
controls. Each row cites executable evidence with typed tokens:

  scenario:<id>              a decision-benchmark scenario in evals/scenarios/
  realfault:<id>             a real-fault benchmark entry in evals/realfaults/manifest.toml
  test:<file>::<function>    a test function
  drill:<language>/<name>    a gate drill in scripts/drill.py
  job:<workflow>#<job>       a job in .github/workflows/<workflow>
  control:<id>               a scorecard control
  cmd:<subcommand>           a `golden` subcommand
  file:<path>                any file in the repository

A renamed scenario or a deleted job fails this test until the matrix is fixed,
so the alignment claim cannot quietly rot.
"""

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

from golden import cli
from golden import scorecard as sc

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "alignment.md"
TOKEN = re.compile(r"`(scenario|realfault|test|drill|job|control|cmd|file):([^`]+)`")
ROW = re.compile(r"^\| *(\d+) *\|")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def rows():
    """(row number, row text) for every numbered row of every matrix table."""
    return [
        (int(m[1]), line)
        for line in MATRIX.read_text(encoding="utf-8").splitlines()
        if (m := ROW.match(line))
    ]


def tokens():
    return sorted({(kind, ref) for _, line in rows() for kind, ref in TOKEN.findall(line)})


@pytest.fixture(scope="module")
def known():
    runner = _load("alignment_evals_runner", ROOT / "evals" / "runner.py")
    drill = _load("alignment_drill", ROOT / "scripts" / "drill.py")
    manifest = ROOT / "evals" / "realfaults" / "manifest.toml"
    faults = (
        {b["id"] for b in tomllib.loads(manifest.read_text(encoding="utf-8")).get("bug", [])}
        if manifest.exists()
        else set()
    )
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if a.dest == "command").choices
    return {
        "scenario": {s.id for s in runner.load()},
        "realfault": faults,
        "drill": {f"{lang}/{name}" for lang in drill.FEATURES for name in drill.EXPECTED},
        "control": {c.id for c in sc.CONTROLS},
        "cmd": set(commands),
    }


def test_matrix_rows_are_numbered_and_cite_evidence():
    numbered = rows()
    assert len(numbered) >= 25
    assert [n for n, _ in numbered] == list(range(1, len(numbered) + 1))
    for n, line in numbered:
        assert TOKEN.search(line) or "Not adopted" in line, f"row {n} cites no evidence"
        assert "http" in line or "](" in line, f"row {n} cites no source"


@pytest.mark.parametrize(("kind", "ref"), tokens(), ids=str)
def test_evidence_exists(kind, ref, known):
    if kind in known:
        assert ref in known[kind], f"{kind}:{ref} is not in the repository"
    elif kind == "test":
        path, _, function = ref.partition("::")
        text = (ROOT / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(function)}\(", text, re.MULTILINE), ref
    elif kind == "job":
        workflow, _, job = ref.partition("#")
        text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        jobs = text[text.index("\njobs:") :]
        assert re.search(rf"^  {re.escape(job)}:", jobs, re.MULTILINE), ref
    else:
        assert (ROOT / ref).exists(), ref
