"""Which source code a change puts at stake: its changed lines, plus what its test edits touch.

Diff-scoped gates look at changed source lines. A pull request that only edits
tests (weakens an assertion, deletes a test case or a whole test file) changes no
source line, so mutation testing would have nothing to check and the weaker
tests would merge unchallenged. To close that gap, every source function that a
changed or removed test line *names* is added to the mutation scope, in the
source files that test references.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from golden import gitdiff
from golden.globmatch import match_any
from golden.presets import Preset, Range

IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


def _merge(ranges: list[Range]) -> list[Range]:
    merged: list[Range] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def tested_functions(
    preset: Preset,
    test_edits: dict[str, str],
    sources: list[str],
    read: Callable[[str], str],
) -> dict[str, list[Range]]:
    """Ranges of the source functions named in the test edits, per source file.

    `test_edits` maps a changed test file to the text of its added and removed
    lines; a deleted test file has only removed lines, which then stand in for
    its content when deciding which sources it references.
    """
    names = {test: set(IDENTIFIER.findall(edit)) for test, edit in test_edits.items()}
    texts = {test: read(test) or edit for test, edit in test_edits.items()}
    result: dict[str, list[Range]] = {}
    for source in sources:
        wanted = set().union(
            *(names[t] for t in test_edits if preset.references(source, t, texts[t]))
        )
        if not wanted:
            continue
        functions = preset.function_ranges(read(source))
        ranges = [r for name in sorted(wanted & set(functions)) for r in functions[name]]
        if ranges:
            result[source] = _merge(ranges)
    return result


def mutation_lines(base: str, preset: Preset, exempt: tuple[str, ...]) -> dict[str, list[Range]]:
    """Line ranges to mutate per source file: changed lines plus test-touched functions."""
    lines = gitdiff.changed_lines(base)
    edits = {p: t for p, t in gitdiff.changed_text(base).items() if preset.is_test(p)}
    if not edits:
        return lines
    sources = [
        p for p in gitdiff.tracked_files() if preset.is_source(p) and not match_any(p, exempt)
    ]
    for source, ranges in tested_functions(preset, edits, sources, gitdiff.read_head).items():
        lines[source] = _merge([*lines.get(source, []), *ranges])
    return lines
