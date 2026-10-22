"""Gate-effectiveness drills: does each gate catch what it claims to?

For each language, copy the example project into a scratch git repository
nested in a subdirectory (like a monorepo), add a small feature on a branch in
three ways, and run the real gate commands:

  no-tests  the feature ships without tests
  weak      a test executes the feature but asserts nothing useful
  strong    boundary-value tests that pin the behaviour

Expected verdicts are asserted, so this doubles as an end-to-end test of the
pipeline logic. Run: uv run python scripts/drill.py [python|node|go ...]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from golden.plan import DEFAULTS  # noqa: E402

GOLDEN = [sys.executable, "-m", "golden"]


@dataclass(frozen=True)
class Feature:
    source: str
    code: str
    test_file: str
    weak: str
    strong: str


FEATURES = {
    "python": Feature(
        source="src/pricing/core.py",
        code='''

def bulk_discount_percent(quantity: int) -> int:
    """Tiered discount: 10+ units 5%, 50+ units 10%."""
    if quantity >= 50:
        return 10
    if quantity >= 10:
        return 5
    return 0
''',
        test_file="tests/test_bulk.py",
        weak="""from pricing.core import bulk_discount_percent


def test_bulk_runs():
    for qty in (1, 10, 50):
        assert bulk_discount_percent(qty) is not None
""",
        strong="""import pytest

from pricing.core import bulk_discount_percent


@pytest.mark.parametrize(("qty", "pct"), [(1, 0), (9, 0), (10, 5), (49, 5), (50, 10)])
def test_tiers(qty, pct):
    assert bulk_discount_percent(qty) == pct
""",
    ),
    "node": Feature(
        source="src/pricing.ts",
        code="""
export function bulkDiscountPercent(quantity: number): number {
  if (quantity >= 50) return 10;
  if (quantity >= 10) return 5;
  return 0;
}
""",
        test_file="src/bulk.test.ts",
        weak="""import { expect, it } from "vitest";
import { bulkDiscountPercent } from "./pricing.js";

it("runs", () => {
  for (const q of [1, 10, 50]) expect(bulkDiscountPercent(q)).toBeDefined();
});
""",
        strong="""import { expect, it } from "vitest";
import { bulkDiscountPercent } from "./pricing.js";

it.each([
  [1, 0],
  [9, 0],
  [10, 5],
  [49, 5],
  [50, 10],
])("quantity %i gets %i%%", (q, pct) => {
  expect(bulkDiscountPercent(q)).toBe(pct);
});
""",
    ),
    "go": Feature(
        source="pricing/pricing.go",
        code="""
// BulkDiscountPercent is a tiered discount by quantity.
func BulkDiscountPercent(quantity int) int {
	if quantity >= 50 {
		return 10
	}
	if quantity >= 10 {
		return 5
	}
	return 0
}
""",
        test_file="pricing/bulk_test.go",
        weak="""package pricing

import "testing"

func TestBulkRuns(t *testing.T) {
	for _, q := range []int{1, 10, 50} {
		_ = BulkDiscountPercent(q)
	}
}
""",
        strong="""package pricing

import "testing"

func TestBulkTiers(t *testing.T) {
	for q, want := range map[int]int{1: 0, 9: 0, 10: 5, 49: 5, 50: 10} {
		if got := BulkDiscountPercent(q); got != want {
			t.Fatalf("BulkDiscountPercent(%d) = %d, want %d", q, got, want)
		}
	}
}
""",
    ),
}

# Gate verdicts each scenario must produce (True = passes).
EXPECTED = {
    "no-tests": {"diff-coverage": False, "presence": False, "mutation": False},
    "weak": {"diff-coverage": True, "presence": True, "mutation": False},
    "strong": {"diff-coverage": True, "presence": True, "mutation": True},
}


def sh(cmd: str | list[str], cwd: Path, *, env: dict[str, str] | None = None) -> int:
    shell = isinstance(cmd, str)
    argv = ["bash", "-o", "noglob", "-euo", "pipefail", "-c", cmd] if shell else cmd
    result = subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if os.environ.get("DRILL_VERBOSE"):
        print(result.stdout[-3000:], result.stderr[-3000:], sep="\n")
    return result.returncode


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=drill", "-c", "user.email=drill@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def scenario(language: str, name: str, workdir: Path) -> dict[str, bool]:
    feature, cmds = FEATURES[language], DEFAULTS[language]
    repo = workdir / f"{language}-{name}"
    project = repo / "services" / language
    example = ROOT / "examples" / language
    shutil.copytree(
        example,
        project,
        ignore=shutil.ignore_patterns(
            "mutants", "coverage*", "reports", ".stryker-tmp", "node_modules", ".venv"
        ),
    )
    if (example / "node_modules").exists():
        (project / "node_modules").symlink_to(example / "node_modules")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "feature")

    with (project / feature.source).open("a", encoding="utf-8") as fh:
        fh.write(feature.code)
    if name != "no-tests":
        (project / feature.test_file).write_text(feature.weak if name == "weak" else feature.strong)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)

    results: dict[str, bool] = {}
    if sh(cmds["test-command"], project) != 0:
        raise RuntimeError(f"{language}/{name}: tests failed to run")
    results["diff-coverage"] = (
        sh(
            [
                *GOLDEN,
                "diff-coverage",
                "--base",
                base,
                "--preset",
                language,
                cmds["coverage-report"],
            ],
            project,
        )
        == 0
    )
    results["presence"] = (
        sh([*GOLDEN, "presence", "--base", base, "--preset", language], project) == 0
    )
    targets = subprocess.run(
        [
            *GOLDEN,
            "changed",
            "--base",
            base,
            "--preset",
            language,
            "--kind",
            "mutation-targets",
            "--sep",
            "comma" if language == "node" else "space",
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env = {"GOLDEN_MUTATE": targets, "GOLDEN_BASE": base}
    if sh(cmds["mutation-command"], project, env=env) != 0:
        raise RuntimeError(f"{language}/{name}: mutation tool failed")
    results["mutation"] = (
        sh(
            [
                *GOLDEN,
                "mutation",
                cmds["mutation-report"],
                "--format",
                cmds["mutation-format"],
                "--base",
                base,
            ],
            project,
        )
        == 0
    )
    return results


def prepare(language: str) -> None:
    example = ROOT / "examples" / language
    if language == "node" and not (example / "node_modules").exists():
        subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=example, check=True)


def main(languages: list[str]) -> int:
    failures = []
    rows = [
        "| Language | Scenario | Diff coverage | Test presence | Mutation | As expected |",
        "|---|---|---|---|---|---|",
    ]
    with tempfile.TemporaryDirectory(prefix="golden-drill-") as tmp:
        for language in languages:
            prepare(language)
            for name, expected in EXPECTED.items():
                got = scenario(language, name, Path(tmp))
                ok = got == expected
                failures += [] if ok else [f"{language}/{name}: expected {expected}, got {got}"]
                cells = " | ".join("pass" if got[g] else "**block**" for g in expected)
                rows.append(f"| {language} | {name} | {cells} | {'✅' if ok else '❌'} |")
    report = "\n".join(["### Gate drills", "", *rows])
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    for f in failures:
        print(f"::error::{f}")
    print(json.dumps({"failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or list(FEATURES)))
