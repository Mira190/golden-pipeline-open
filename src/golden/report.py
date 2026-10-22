"""Console output, GitHub annotations and job-summary helpers."""

from __future__ import annotations

import os
from pathlib import Path


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotate(level: str, message: str, *, file: str | None = None, line: int | None = None) -> None:
    """Emit a GitHub workflow command (`::error file=...::msg`)."""
    props = []
    if file:
        props.append(f"file={file}")
    if line:
        props.append(f"line={line}")
    prefix = f"::{level} {','.join(props)}::" if props else f"::{level}::"
    print(prefix + _escape(message))


def summary(markdown: str) -> None:
    """Print markdown and append it to the job summary when running in Actions."""
    print(markdown)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip() + "\n\n")


def verdict(gate: str, *, passed: bool, detail: str, waived: bool = False) -> int:
    """Report a gate result and return the process exit code."""
    if passed:
        icon, status, code = "✅", "passed", 0
    elif waived:
        icon, status, code = "⚠️", "failed (waived)", 0
    else:
        icon, status, code = "❌", "failed", 1
    summary(f"#### {icon} {gate}: {status}\n\n{detail}")
    return code
