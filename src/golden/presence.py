"""Gate: every changed source file must ship with a changed test that exercises it.

Diff coverage catches uncovered *lines*. This gate catches the subtler case
where new behaviour is executed only incidentally by old tests and nobody
wrote a test for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from golden.globmatch import match_any
from golden.presets import Preset


@dataclass(frozen=True)
class Finding:
    source: str
    reason: str


def evaluate(
    changed: Iterable[str],
    preset: Preset,
    read: Callable[[str], str],
    exempt: tuple[str, ...] = (),
) -> list[Finding]:
    changed = list(changed)
    sources = [p for p in changed if preset.is_source(p) and not match_any(p, exempt)]
    tests = [p for p in changed if preset.is_test(p)]
    texts = {t: read(t) for t in tests}

    findings = []
    for src in sources:
        if not tests:
            findings.append(Finding(src, "no test files changed"))
        elif not any(preset.references(src, t, texts[t]) for t in tests):
            findings.append(Finding(src, "no changed test references this file"))
    return findings
