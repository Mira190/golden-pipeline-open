"""Mutation score from mutmut, StrykerJS or gremlins reports.

All three are normalised to Stryker's definition:
    score = detected / valid = (killed + timeout) / (killed + timeout + survived + no_coverage)
Compile/runtime errors, ignored and not-yet-run mutants are excluded from the
score; but a run in which *no* mutant got a verdict fails closed, like a missing
report, because it proves nothing.

Stryker and gremlins reports carry file and line for every mutant, so they can
be narrowed to the lines a change touched; mutmut is narrowed up front by
selecting the changed functions. Its stats file has totals only, so the surviving
mutants are read from the per-mutant results mutmut leaves next to it in `mutants/`
and placed on their source lines (see "mutmut survivors" below). They are shown to
the reviewer; the score, and so the verdict, still comes from the stats file.

They can also be narrowed to the lines a test should care about. Google found
developers judged most mutants unproductive until *arid* nodes (logging, debug
output) were suppressed: productive mutants rose from 15% to 89% ("Practical
Mutation Testing at Scale", IEEE TSE 2021). Mutants on a line that is nothing
but a logging call are therefore dropped before scoring, and counted.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    # mutmut only. Survivors that could not be placed on a source line, by mutant name, and
    # why the per-mutant results could not be read at all. They explain what is not shown, so
    # they are no part of the tally's identity.
    unlocated: tuple[str, ...] = field(default=(), compare=False)
    locate_failure: str = field(default="", compare=False)

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

    @property
    def survivors_note(self) -> str:
        """Markdown for the gate detail about mutmut survivors that are not annotated."""
        note = ""
        if self.locate_failure:
            note += (
                f"\n\nSurviving mutants could not be located ({self.locate_failure}); "
                "the verdict rests on mutmut's stats file alone."
            )
        if self.unlocated:
            note += "\n\nSurviving mutants without a source line (not annotated):\n\n" + "\n".join(
                f"- `{name}`" for name in self.unlocated[:50]
            )
        return note

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


# ------------------------------------------------------------ mutmut survivors
# `mutmut export-cicd-stats` writes totals only. Per mutant, mutmut 3.x leaves two things in
# the `mutants/` directory it ran in. Both are read here, and mutmut is never run again:
#
#   mutants/<path>.meta  JSON. `exit_code_by_key` maps each mutant (`pkg.mod.x_f__mutmut_3`, or
#                        `pkg.mod.xǁClassǁmethod__mutmut_3` for a method) to the exit code of
#                        its test run.
#   mutants/<path>       the module with every mutated function written out three ways:
#                        the trampoline `f`, a verbatim copy `x_f__mutmut_orig`, and
#                        `x_f__mutmut_<n>`, the same text with exactly one mutation.
#
# What the exit codes mean is `mutmut.stats.status_by_exit_code` (mutmut 3.8.0):
#   0 survived | 1, 3 killed | 5, 33 no tests | 36, 24, -24, 152, 255 timeout | 34 skipped
#   35 and any code not listed: suspicious | 37 caught by the type checker | 2 interrupted
#   -9, -11 segfault | null not checked yet
# 5 is pytest's "no tests collected"; 33 is what `mutmut.__main__._run` writes when no test
# reaches the function. The score counts "no tests" as not covered, as Stryker counts
# NoCoverage, and the other formats list those mutants with the survivors, so this does too.
MUTMUT_LIVED = {0: SURVIVED, 5: NO_COVERAGE, 33: NO_COVERAGE}
_MUTANT = re.compile(
    r"(?P<mangled>x(?:_(?P<function>\w+)|ǁ(?P<owner>[^ǁ]+)ǁ(?P<method>\w+)))__mutmut_[0-9]+"
)
# A function as the line its `def` is on (not its decorators) and its own lines.
_Def = tuple[int, list[str]]


def _defs(path: Path) -> dict[tuple[str, str], _Def]:
    """Top-level functions and methods of top-level classes by (class, name): what mutmut
    mutates. Empty when the file cannot be read or parsed."""
    lines = _source_lines(str(path))
    if lines is None:
        return {}
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError):
        return {}
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    scopes = [("", tree.body)]
    scopes += [(node.name, node.body) for node in tree.body if isinstance(node, ast.ClassDef)]
    return {
        (owner, node.name): (node.lineno, lines[node.lineno - 1 : node.end_lineno or node.lineno])
        for owner, body in scopes
        for node in body
        if isinstance(node, kinds)
    }


def _renamed(lines: list[str], old: str, new: str) -> list[str]:
    """A function's lines with its `def` line spelled with another name."""
    return [lines[0].replace(old, new, 1), *lines[1:]]


def _first_change(
    key: str, source: dict[tuple[str, str], _Def], mutated: dict[tuple[str, str], _Def]
) -> int | None:
    """The source line of the first line a mutant changes, or None when that cannot be told.

    The mutated file holds the function's verbatim copy and the mutant, so the first line on
    which they differ is the mutation. Its offset from the `def` line, added to the `def` line
    of the function in `source`, is the line in the file as it is now. That is right only when
    the copy *is* the source function, character for character. Anything else (the file was
    edited after the run, the function is not there, the mutant differs by no line) is None:
    a line is never guessed.

    Limits: a mutation inside a multi-line statement is placed on the line of the changed
    token, not on the first line of the statement; a mutant that only appends lines is not
    placed; only top-level functions and methods of top-level classes exist, as for mutmut.
    """
    generated = key.rpartition(".")[2]
    found = _MUTANT.fullmatch(generated)
    if found is None:
        return None
    owner, name = found["owner"] or "", found["function"] or found["method"]
    orig_name = found["mangled"] + "__mutmut_orig"
    here = source.get((owner, name))
    orig = mutated.get((owner, orig_name))
    mutant = mutated.get((owner, generated))
    if here is None or orig is None or mutant is None:
        return None
    start, current = here
    original = _renamed(orig[1], orig_name, name)
    changed = _renamed(mutant[1], generated, name)
    if original != current:
        return None
    for offset, line in enumerate(original):
        if offset >= len(changed) or line != changed[offset]:
            return start + offset
    return None


def _exit_codes(meta: Path) -> dict[str, int | None] | None:
    """`exit_code_by_key` of a mutmut .meta file; None when it is not what mutmut writes."""
    try:
        codes = json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(codes, dict) or not all(c is None or type(c) is int for c in codes.values()):
        return None
    return codes


def _read_survivors(
    report: Path, lines: dict[str, list[Range]] | None
) -> tuple[list[Mutant], list[str]]:
    """The surviving mutants of a mutmut run within `lines`, as (on a source line, name only).

    mutmut has no mutator names, so a mutant's key stands in for it (`mutmut show KEY` prints
    the change). Files with no line in `lines` are not read. A mutant that cannot be placed is
    listed by name when its file is in scope. Raises ValueError, saying why, when the
    per-mutant results cannot be read: all or nothing, as a partial list would read as complete.
    """
    tree = report.absolute().parent
    if tree.name != "mutants":
        raise ValueError("the report is not in a mutants/ directory")
    metas = sorted(tree.rglob("*.py.meta"))
    if not metas:
        raise ValueError("mutants/ has no .meta files")
    placed: list[Mutant] = []
    named: list[str] = []
    for meta in metas:
        file = meta.relative_to(tree).with_suffix("").as_posix()
        if lines is not None and not lines.get(_relative(file)):
            continue
        codes = _exit_codes(meta)
        if codes is None:
            raise ValueError(f"mutants/{file}.meta is unreadable or malformed")
        lived = {key: MUTMUT_LIVED[code] for key, code in codes.items() if code in MUTMUT_LIVED}
        if not lived:
            continue
        source, mutated = _defs(tree.parent / file), _defs(tree / file)
        for key, status in lived.items():
            line = _first_change(key, source, mutated)
            if line is None:
                named.append(key)
            else:
                placed.append(Mutant(file, line, status, key))
    in_scope = [m for m in placed if lines is None or in_ranges(m, lines)]
    # Stable: mutants on one line keep mutmut's own order (x_f__mutmut_2 before _10).
    return sorted(in_scope, key=lambda m: (m.file, m.line)), named


def _with_survivors(
    tally: Tally, report: Path, lines: dict[str, list[Range]] | None
) -> tuple[Tally, list[Mutant]]:
    """mutmut's tally with its surviving mutants. The tally, and so the verdict, is the stats
    file's alone: results that cannot be read only add a note on what is not shown."""
    try:
        placed, named = _read_survivors(report, lines)
    except ValueError as exc:
        # Worth saying only when the stats file reports something to find.
        failure = str(exc) if tally.survived + tally.no_coverage else ""
        return replace(tally, locate_failure=failure), []
    return replace(tally, unlocated=tuple(named)), placed


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

    For mutmut the tally is the stats file's, and `lines` only narrows the survivors, which
    come from the per-mutant results in the `mutants/` directory the stats file lives in.
    """
    data = _load(path)
    try:
        if fmt == "mutmut":
            return _with_survivors(_mutmut(data), path, lines)
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
