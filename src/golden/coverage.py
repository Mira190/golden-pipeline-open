"""Coverage report parsing (Cobertura XML, LCOV, Go coverprofile) and the ratchet."""

from __future__ import annotations

import json
import re
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


def detect_format(path: Path) -> str:
    if path.suffix == ".xml":
        return "cobertura"
    head = path.read_text(encoding="utf-8")[:200]
    if head.startswith("mode:"):
        return "gocover"
    return "lcov"


def _cobertura(text: str) -> Totals:
    root = ElementTree.fromstring(text)  # noqa: S314  # nosec B314
    if "lines-valid" in root.attrib:
        return Totals(int(root.attrib["lines-covered"]), int(root.attrib["lines-valid"]))
    lines = root.iter("line")
    hits = [int(line.attrib.get("hits", "0")) for line in lines]
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
        prev = blocks.get(block, (0, 0))
        blocks[block] = (int(stmts), max(prev[1], int(count)))
    total = sum(s for s, _ in blocks.values())
    covered = sum(s for s, c in blocks.values() if c > 0)
    return Totals(covered, total)


PARSERS = {"cobertura": _cobertura, "lcov": _lcov, "gocover": _gocover}


def read_totals(path: Path, fmt: str = "auto") -> Totals:
    fmt = detect_format(path) if fmt == "auto" else fmt
    return PARSERS[fmt](path.read_text(encoding="utf-8"))


def read_baseline(path: Path) -> float | None:
    if not path.exists():
        return None
    return float(json.loads(path.read_text(encoding="utf-8"))["line_percent"])


def write_baseline(path: Path, percent: float, sha: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"line_percent": percent, "sha": sha}, indent=2) + "\n")


def ratchet(current: float, baseline: float, tolerance: float) -> tuple[bool, float]:
    delta = round(current - baseline, 2)
    return delta >= -tolerance, delta
