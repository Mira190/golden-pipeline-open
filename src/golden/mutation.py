"""Mutation score from mutmut, StrykerJS or gremlins reports.

All three are normalised to Stryker's definition:
    score = detected / valid = (killed + timeout) / (killed + timeout + survived + no_coverage)
Compile/runtime errors, ignored and not-yet-run mutants are excluded from the
score; but a run in which *no* mutant got a verdict fails closed, like a missing
report, because it proves nothing.

Stryker and gremlins reports carry file and line for every mutant, so they can
be narrowed to the lines a change touched; mutmut is narrowed up front by
selecting the changed functions.

They can also be narrowed to the lines a test should care about. Google found
developers judged most mutants unproductive until *arid* nodes (logging, debug
output) were suppressed: productive mutants rose from 15% to 89% ("Practical
Mutation Testing at Scale", IEEE TSE 2021). Mutants on a line that is nothing
but a logging call are therefore dropped before scoring, and counted.
"""

from __future__ import annotations

import json
import re
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
    # Mutants in scope dropped before scoring because their line is arid.
    arid: int = 0

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

    @property
    def arid_note(self) -> str:
        """A markdown paragraph for the gate detail; empty when nothing was excluded."""
        if not self.arid:
            return ""
        return f"\n\n{self.arid} mutants excluded as arid (logging/debug output)."

    @classmethod
    def of(cls, mutants: list[Mutant], arid: int = 0) -> Tally:
        c = Counter(m.status for m in mutants)
        return cls(c[KILLED], c[TIMEOUT], c[SURVIVED], c[NO_COVERAGE], c[OTHER], arid)


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


# ------------------------------------------------------------------ arid lines
# A line is arid when it is *only* a logging or debug-output call: no reasonable test
# pins its text down, so a surviving mutant there is noise, not a missing assertion.
# The list is deliberately short and conservative. A false "arid" hides a real survivor;
# a false "not arid" costs one extra annotation. So every pattern is anchored to the
# whole line (one statement, one call), never matches a line that also holds other
# logic (`return x || console.log(y)`, `if (a) console.log(b)`, `log(a); b()`), and
# never matches a call whose arguments have side effects (see `_pure_args`).
# What is not matched stays mutated: multi-line calls, trailing comments, `await log()`.
_CALL = r"\((?P<args>.*)\)\s*;?\s*$"

JS_ARID = (
    # console.*: output for humans; no test should assert on it.
    re.compile(r"^\s*console\.(?:log|debug|info|warn|error|trace)" + _CALL),
    # debug(...): the `debug` package, namespaced diagnostics that are off by default.
    re.compile(r"^\s*debug" + _CALL),
    # logger.<level>(...) / log.<level>(...), also `this.logger`: leveled logging calls.
    re.compile(r"^\s*(?:this\.)?(?:logger|log)\.(?:debug|info|warn|error|trace)" + _CALL),
)
GO_ARID = (
    # log.Print*/Fatal*/Panic*: standard-library logging; only the message text is mutated.
    re.compile(r"^\s*log\.(?:Print|Fatal|Panic)(?:f|ln)?" + _CALL),
    # fmt.Print*: stdout diagnostics. Sprintf/Errorf/Fprintf build values a test may check
    # and are NOT matched.
    re.compile(r"^\s*fmt\.Print(?:f|ln)?" + _CALL),
    # slog.<level>(...): structured logging in the standard library.
    re.compile(r"^\s*slog\.(?:Debug|Info|Warn|Error)(?:Context)?" + _CALL),
    # logger.<level>(...), also on a receiver (`s.logger.Infof`): zap, logrus, slog.Logger.
    re.compile(r"^\s*(?:\w+\.)*logger\.(?:Debug|Info|Warn|Error)(?:f|w|ln)?" + _CALL),
)
ARID_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"), JS_ARID),
    ".go": GO_ARID,
}

_STRING = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`""")
# `x++`, `x--`, `x = 1`, `x += 1`, `await f()`, `<-ch`: the call does more than log.
_IMPURE = re.compile(r"\+\+|--|(?<![=!<>])=(?![=>])|\bawait\b|<-")


def _pure_args(args: str) -> bool:
    """The text between a call's outer parentheses only names values: no nested call, no
    template interpolation, no side effect.

    The anchored regexes cannot tell `log(a)` from `log(a) || go(b)`: the greedy group
    swallows `a) || go(b`, and the parenthesis gives that away. A nested call is refused too,
    so `console.log(applyDiscount(x))` cannot hide the mutants of a call that may do work, and
    so is `${...}`, which runs code inside a template string.
    """
    if "${" in args:
        return False
    bare = _STRING.sub('""', args)
    return "(" not in bare and ")" not in bare and not _IMPURE.search(bare)


def is_arid_line(line: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    """The source line is a single logging/debug call, and nothing else."""
    for pattern in patterns:
        found = pattern.match(line)
        if found and _pure_args(found["args"]):
            return True
    return False


def _source_lines(file: str) -> list[str] | None:
    """Lines of a source file in the working directory; None when it cannot be read."""
    try:
        return Path(file).read_text(encoding="utf-8").split("\n")
    except (OSError, ValueError):  # missing, a directory, not UTF-8, a NUL in the name
        return None


def _is_arid(mutant: Mutant, sources: dict[str, list[str] | None]) -> bool:
    patterns = ARID_PATTERNS.get(PurePosixPath(mutant.file).suffix.lower())
    if not patterns:
        return False
    if mutant.file not in sources:
        sources[mutant.file] = _source_lines(mutant.file)
    source = sources[mutant.file]
    # An unreadable file, or a line outside it, never excludes a mutant.
    if source is None or not 1 <= mutant.line <= len(source):
        return False
    return is_arid_line(source[mutant.line - 1], patterns)


def drop_arid(mutants: list[Mutant]) -> tuple[list[Mutant], int]:
    """Mutants that are not on an arid line, and how many were dropped."""
    sources: dict[str, list[str] | None] = {}
    kept = [m for m in mutants if not _is_arid(m, sources)]
    return kept, len(mutants) - len(kept)


def evaluate(
    path: Path,
    fmt: str,
    lines: dict[str, list[Range]] | None = None,
    *,
    arid_filter: bool = True,
) -> tuple[Tally, list[Mutant]]:
    """Tally and surviving mutants, optionally restricted to changed lines.

    Stryker and gremlins mutants on arid lines (see ARID_PATTERNS) are dropped before
    scoring and counted in `Tally.arid`: they are neither detected nor survived.
    """
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
    arid = 0
    if arid_filter:
        mutants, arid = drop_arid(mutants)
    lived = [m for m in mutants if m.status in {SURVIVED, NO_COVERAGE}]
    return Tally.of(mutants, arid), lived
