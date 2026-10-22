"""Coverage report parsing (Cobertura XML, LCOV, Go coverprofile) and the ratchet."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Cobertura is produced by our own test step in the same job; it is not untrusted input.
from xml.etree import ElementTree  # nosec B405


@dataclass(frozen=True)
class Totals:
    covered: int
    total: int

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else round(100 * self.covered / self.total, 2)


def _text(path: Path) -> str:
    """The report's text. A report that is not UTF-8 is an error that names the report."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"coverage report {path} is not UTF-8 text: {exc}") from exc


def detect_format(path: Path) -> str:
    if path.suffix == ".xml":
        return "cobertura"
    head = _text(path)[:200]
    if head.startswith("mode:"):
        return "gocover"
    return "lcov"


def _xml(text: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(text)  # noqa: S314  # nosec B314
    except ElementTree.ParseError as exc:
        raise ValueError(f"unreadable Cobertura report: {exc}") from exc


def _whole(attrib: Mapping[str, str], name: str, default: str | None = None) -> int:
    """An XML attribute as a whole number; a missing or garbled one is an error."""
    value = attrib.get(name, default)
    if value is None:
        raise ValueError(f"the Cobertura attribute {name} is missing")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"the Cobertura attribute {name}={value!r} is not a whole number") from exc


def _cobertura(text: str) -> Totals:
    root = _xml(text)
    if "lines-valid" in root.attrib:
        return Totals(_whole(root.attrib, "lines-covered"), _whole(root.attrib, "lines-valid"))
    hits = [_whole(line.attrib, "hits", "0") for line in root.iter("line")]
    return Totals(sum(1 for h in hits if h > 0), len(hits))


def _lcov(text: str) -> Totals:
    found = sum(int(m) for m in re.findall(r"^LF:(\d+)", text, re.MULTILINE))
    hit = sum(int(m) for m in re.findall(r"^LH:(\d+)", text, re.MULTILINE))
    return Totals(hit, found)


def _gocover(text: str) -> Totals:
    # file:startLine.col,endLine.col numStatements hitCount; blocks may repeat
    # across packages when -coverpkg is used, so dedupe by block, keeping max hits.
    blocks: dict[str, tuple[int, int]] = {}
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        block, stmts, count = line.rsplit(" ", 2)
        if int(stmts) < 0:
            raise ValueError(f"the block {block} has {stmts} statements")
        prev = blocks.get(block, (0, 0))
        blocks[block] = (int(stmts), max(prev[1], int(count)))
    total = sum(s for s, _ in blocks.values())
    covered = sum(s for s, c in blocks.values() if c > 0)
    return Totals(covered, total)


PARSERS = {"cobertura": _cobertura, "lcov": _lcov, "gocover": _gocover}


def read_totals(path: Path, fmt: str = "auto") -> Totals:
    """Fails closed: a report with no coverable lines means the tests did not run, and one that
    covers more lines than it has (or fewer than none) is not a report."""
    fmt = detect_format(path) if fmt == "auto" else fmt
    text = _text(path)
    try:
        totals = PARSERS[fmt](text)
    except ValueError as exc:
        raise ValueError(f"malformed {fmt} coverage report {path}: {exc}") from exc
    if totals.total == 0:
        raise ValueError(f"coverage report {path} has no coverable lines; did the tests run?")
    if not 0 <= totals.covered <= totals.total:
        raise ValueError(
            f"coverage report {path} is inconsistent: {totals.covered} of {totals.total} "
            "lines covered"
        )
    return totals


def measured_files(path: Path) -> set[str]:
    """Every file a coverage report knows about, as the report spells it (POSIX).

    Cobertura names are also joined with each `<source>` root, because
    coverage.py writes `pkg/a.py` under `<source>src</source>`.
    """
    text = _text(path)
    fmt = detect_format(path)
    if fmt == "lcov":
        return {m.strip() for m in re.findall(r"^SF:(.+)$", text, re.MULTILINE)}
    if fmt == "gocover":
        return {line.rsplit(":", 1)[0] for line in text.splitlines()[1:] if ":" in line}
    root = _xml(text)
    roots = [(s.text or "").strip() for s in root.iter("source")]
    names = {c.attrib["filename"] for c in root.iter("class") if "filename" in c.attrib}
    return names | {f"{r.rstrip('/')}/{n}" for r in roots if r for n in names}


def is_measured(file: str, measured: set[str]) -> bool:
    """Whether `file` (relative to the project) appears in the report under any spelling."""
    return any(m == file or m.endswith("/" + file) for m in measured)


def read_baseline(path: Path) -> float | None:
    """The line coverage of the default branch; None when no baseline was saved yet.

    A value that is not a percentage is an error: a negative baseline would let every change
    through the ratchet, and `-Infinity` or a huge number would make it fail (or pass) forever.
    """
    if not path.exists():
        return None
    try:
        percent = json.loads(path.read_text(encoding="utf-8"))["line_percent"]
    except (ValueError, RecursionError, KeyError, TypeError) as exc:
        # ValueError: not JSON, or not UTF-8; RecursionError: nested a few thousand levels deep.
        raise ValueError(f"coverage baseline {path} is malformed: {exc!r}") from exc
    if isinstance(percent, bool) or not isinstance(percent, int | float) or not 0 <= percent <= 100:
        raise ValueError(f"coverage baseline {path} is malformed: {percent!r} is not a percentage")
    return float(percent)


def write_baseline(path: Path, percent: float, sha: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"line_percent": percent, "sha": sha}, indent=2) + "\n")


def ratchet(current: float, baseline: float, tolerance: float) -> tuple[bool, float]:
    delta = round(current - baseline, 2)
    return delta >= -tolerance, delta
