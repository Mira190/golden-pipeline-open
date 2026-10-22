"""Mutation score from mutmut, StrykerJS or gremlins reports.

All three are normalised to Stryker's definition:
    score = detected / valid = (killed + timeout) / (killed + timeout + survived + no_coverage)
Compile/runtime errors, ignored and not-yet-run mutants are excluded from the
score; but a run in which *no* mutant got a verdict fails closed, like a missing
report, because it proves nothing.

Stryker and gremlins reports carry file and line for every mutant, so they can
be narrowed to the lines a change touched; mutmut is narrowed up front by
selecting the changed functions.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

Range = tuple[int, int]

# Normalised statuses. IGNORED is a deliberate, reviewable opt-out (a disable
# comment); OTHER is a mutant that never got a verdict (compile error, not run).
KILLED, TIMEOUT, SURVIVED, NO_COVERAGE, IGNORED, OTHER = (
    "killed",
    "timeout",
    "survived",
    "no-coverage",
    "ignored",
    "other",
)
STRYKER = {
    "Killed": KILLED,
    "Timeout": TIMEOUT,
    "Survived": SURVIVED,
    "NoCoverage": NO_COVERAGE,
    "Ignored": IGNORED,
}
GREMLINS = {
    "KILLED": KILLED,
    "TIMED OUT": TIMEOUT,
    "LIVED": SURVIVED,
    "NOT COVERED": NO_COVERAGE,
    "SKIPPED": IGNORED,
}
# When most mutants "time out", the tool's timeout is miscalibrated and the
# run proves nothing; counting those as detected would pass a broken run.
MAX_TIMEOUT_SHARE = 0.5


@dataclass(frozen=True)
class Mutant:
    file: str
    line: int
    status: str
    mutator: str = ""


@dataclass(frozen=True)
class Tally:
    killed: int = 0
    timeout: int = 0
    survived: int = 0
    no_coverage: int = 0
    # Mutants in scope that never produced a verdict (compile errors, not run).
    invalid: int = 0

    @property
    def valid(self) -> int:
        return self.killed + self.timeout + self.survived + self.no_coverage

    @property
    def score(self) -> float | None:
        if self.valid == 0:
            return None
        return round(100 * (self.killed + self.timeout) / self.valid, 2)

    @property
    def unreliable(self) -> bool:
        return self.valid > 0 and self.timeout / self.valid > MAX_TIMEOUT_SHARE

    @property
    def inconclusive(self) -> bool:
        """Mutants were generated but none got a verdict: the run proves nothing."""
        return self.valid == 0 and self.invalid > 0

    @classmethod
    def of(cls, mutants: list[Mutant]) -> Tally:
        c = Counter(m.status for m in mutants)
        return cls(c[KILLED], c[TIMEOUT], c[SURVIVED], c[NO_COVERAGE], c[OTHER])


def _relative(name: str, project_root: str | None = None) -> str:
    """Reports may name files `./src/a.ts` or by absolute path (Stryker.NET, PIT);
    git names them `src/a.ts`, relative to the project."""
    path = PurePosixPath(name)
    if not path.is_absolute():
        return path.as_posix()
    for root in (project_root, Path.cwd().as_posix()):
        if root and path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    return name


def _elements(data: dict[str, Any]) -> list[Mutant]:
    # mutation-testing-report-schema (StrykerJS, Stryker.NET, Stryker4s, ...):
    # files.<path>.mutants[]
    root = data.get("projectRoot")
    return [
        Mutant(
            _relative(name, root),
            int(m["location"]["start"]["line"]),
            STRYKER.get(m["status"], OTHER),
            m.get("mutatorName", ""),
        )
        for name, f in data.get("files", {}).items()
        for m in f.get("mutants", [])
    ]


def _gremlins(data: dict[str, Any]) -> list[Mutant]:
    return [
        Mutant(
            _relative(f["file_name"]),
            int(m["line"]),
            GREMLINS.get(m["status"], OTHER),
            m.get("type", ""),
        )
        for f in data.get("files", [])
        for m in f.get("mutations", [])
    ]


def _mutmut(data: dict[str, Any]) -> Tally:
    # `mutmut export-cicd-stats` aggregates; suspicious/segfault behave like timeouts.
    # Fails closed like a missing report: an interrupted run proves nothing about the
    # mutants it never finished. (`total` cannot tell, it counts unselected mutants too.)
    if int(data.get("check_was_interrupted_by_user", 0)):
        raise ValueError("mutmut run was interrupted; the mutation report is incomplete")
    return Tally(
        killed=int(data.get("killed", 0)),
        timeout=int(data.get("timeout", 0))
        + int(data.get("suspicious", 0))
        + int(data.get("segfault", 0)),
        survived=int(data.get("survived", 0)),
        no_coverage=int(data.get("no_tests", 0)),
    )


LINE_PARSERS: dict[str, Callable[[dict[str, Any]], list[Mutant]]] = {
    "gremlins": _gremlins,
    "mutation-testing-elements": _elements,
    "stryker": _elements,
}
PARSERS = (*LINE_PARSERS, "mutmut")


def _load(path: Path) -> dict[str, Any]:
    """Fails closed: a missing report means the mutation tool did not run."""
    if not path.exists():
        raise FileNotFoundError(f"mutation report {path} not found; did the mutation tool run?")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def in_ranges(mutant: Mutant, lines: dict[str, list[Range]]) -> bool:
    return any(lo <= mutant.line <= hi for lo, hi in lines.get(_relative(mutant.file), []))


def evaluate(
    path: Path, fmt: str, lines: dict[str, list[Range]] | None = None
) -> tuple[Tally, list[Mutant]]:
    """Tally and surviving mutants, optionally restricted to changed lines."""
    data = _load(path)
    try:
        if fmt == "mutmut":
            return _mutmut(data), []
        mutants = LINE_PARSERS[fmt](data)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # Schema drift after a tool upgrade must fail closed, with a readable reason.
        raise ValueError(f"malformed {fmt} report {path}: {exc!r}") from exc
    if lines is not None:
        mutants = [m for m in mutants if in_ranges(m, lines)]
    lived = [m for m in mutants if m.status in {SURVIVED, NO_COVERAGE}]
    return Tally.of(mutants), lived
