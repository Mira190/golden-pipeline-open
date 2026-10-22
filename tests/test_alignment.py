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

The standards crosswalk cites each standard's own identifiers, checked against
the published lists below:

  ssdf:<task>                a NIST SP 800-218 (SSDF v1.1) task, e.g. PW.8.2
  slsa:<track>-L<n>          a SLSA v1.2 level
  openssf:<check>            an OpenSSF Scorecard check
  samm:<stream>              an OWASP SAMM v2 stream question, e.g. I-SB-B-3
  dora:<capability>          a DORA capability page slug
  rows:<first>-<last>        matrix rows the crosswalk line covers

A renamed scenario, a deleted job or a withdrawn standard ID fails this test
until the matrix is fixed, so the alignment claim cannot quietly rot.
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
KINDS = "scenario|realfault|test|drill|job|control|cmd|file|ssdf|slsa|openssf|samm|dora|rows"
TOKEN = re.compile(rf"`({KINDS}):([^`<][^`]*)`")  # `kind:<placeholder>` is the legend
ROW = re.compile(r"^\| *(\d+) *\|")

# NIST SP 800-218 v1.1 (February 2022), tables in section 3. Tasks that only say
# "Moved to ..." (PW.3.1, PW.3.2, PW.4.3, PW.4.5, PW.5.2) are deliberately absent.
SSDF = {
    *(f"PO.1.{n}" for n in (1, 2, 3)),
    *(f"PO.2.{n}" for n in (1, 2, 3)),
    *(f"PO.3.{n}" for n in (1, 2, 3)),
    *(f"PO.4.{n}" for n in (1, 2)),
    *(f"PO.5.{n}" for n in (1, 2)),
    "PS.1.1",
    "PS.2.1",
    "PS.3.1",
    "PS.3.2",
    *(f"PW.1.{n}" for n in (1, 2, 3)),
    "PW.2.1",
    "PW.4.1",
    "PW.4.2",
    "PW.4.4",
    "PW.5.1",
    *(f"PW.6.{n}" for n in (1, 2)),
    *(f"PW.7.{n}" for n in (1, 2)),
    *(f"PW.8.{n}" for n in (1, 2)),
    *(f"PW.9.{n}" for n in (1, 2)),
    *(f"RV.1.{n}" for n in (1, 2, 3)),
    *(f"RV.2.{n}" for n in (1, 2)),
    *(f"RV.3.{n}" for n in (1, 2, 3, 4)),
}
# SLSA v1.2: Build L0-L3, Source L1-L4.
SLSA = {*(f"build-L{n}" for n in range(4)), *(f"source-L{n}" for n in range(1, 5))}
# github.com/ossf/scorecard docs/checks.md.
OPENSSF = {
    "Binary-Artifacts",
    "Branch-Protection",
    "CI-Tests",
    "CII-Best-Practices",
    "Code-Review",
    "Contributors",
    "Dangerous-Workflow",
    "Dependency-Update-Tool",
    "Fuzzing",
    "License",
    "Maintained",
    "Packaging",
    "Pinned-Dependencies",
    "SAST",
    "SBOM",
    "Security-Policy",
    "Signed-Releases",
    "Token-Permissions",
    "Vulnerabilities",
    "Webhooks",
}
# OWASP SAMM v2: business function, practice, stream A or B, maturity level 1-3.
SAMM_PRACTICES = {
    "G": ("SM", "PC", "EG"),
    "D": ("TA", "SR", "SA"),
    "I": ("SB", "SD", "DM"),
    "V": ("AA", "RT", "ST"),
    "O": ("IM", "EM", "OM"),
}
SAMM = {
    f"{function}-{practice}-{stream}-{level}"
    for function, practices in SAMM_PRACTICES.items()
    for practice in practices
    for stream in "AB"
    for level in (1, 2, 3)
}
# dora.dev/capabilities/<slug>/.
DORA = {
    "code-maintainability",
    "continuous-delivery",
    "continuous-integration",
    "deployment-automation",
    "pervasive-security",
    "streamlining-change-approval",
    "test-automation",
    "trunk-based-development",
    "version-control",
    "working-in-small-batches",
}


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
    return sorted(set(TOKEN.findall(MATRIX.read_text(encoding="utf-8"))))


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
        "ssdf": SSDF,
        "slsa": SLSA,
        "openssf": OPENSSF,
        "samm": SAMM,
        "dora": DORA,
    }


def test_matrix_rows_are_numbered_and_cite_evidence():
    numbered = rows()
    assert len(numbered) >= 25
    assert [n for n, _ in numbered] == list(range(1, len(numbered) + 1))
    for n, line in numbered:
        assert TOKEN.search(line) or "Not adopted" in line, f"row {n} cites no evidence"
        assert "http" in line or "](" in line, f"row {n} cites no source"


def test_standard_vocabularies_exclude_withdrawn_and_malformed_ids():
    assert len(SSDF) == 42
    assert not {"PW.3.1", "PW.3.2", "PW.4.3", "PW.4.5", "PW.5.2"} & SSDF
    assert len(OPENSSF) == 20
    assert len(SAMM) == 15 * 2 * 3
    assert "I-SB-B-3" in SAMM
    assert "I-SB-C-1" not in SAMM
    assert "build-L3" in SLSA
    assert "build-L4" not in SLSA
    assert "source-L0" not in SLSA


def test_crosswalk_cites_every_standard():
    kinds = {kind for kind, _ in tokens()}
    assert {"ssdf", "slsa", "openssf", "samm", "dora", "rows"} <= kinds


@pytest.mark.parametrize(("kind", "ref"), tokens(), ids=str)
def test_evidence_exists(kind, ref, known):
    if kind in known:
        assert ref in known[kind], f"{kind}:{ref} is not in the repository or the standard"
    elif kind == "rows":
        first, _, last = ref.partition("-")
        assert 1 <= int(first) <= int(last) <= len(rows()), ref
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
