"""What changed on this branch, relative to the current directory."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
Range = tuple[int, int]


class GitError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise GitError(exc.stderr.strip() or str(exc)) from exc


def changed_files(base: str, *, cwd: Path | None = None) -> list[str]:
    """Files added/copied/modified/renamed since merge-base(base, HEAD).

    Paths are relative to `cwd` and limited to files beneath it, so the gates
    work for a project living in a subdirectory of a monorepo.
    """
    out = _git("diff", "--name-only", "--diff-filter=ACMR", "--relative", f"{base}...HEAD", cwd=cwd)
    return sorted(line for line in out.splitlines() if line)


def changed_lines(base: str, *, cwd: Path | None = None) -> dict[str, list[Range]]:
    """Changed (added or modified) line ranges per file, inclusive, in HEAD's numbering."""
    out = _git(
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--diff-filter=ACMR",
        "--relative",
        f"{base}...HEAD",
        cwd=cwd,
    )
    result: dict[str, list[Range]] = {}
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif current and (m := HUNK.match(line)):
            start, count = int(m[1]), int(m[2]) if m[2] is not None else 1
            if count:
                result.setdefault(current, []).append((start, start + count - 1))
    return result


def repo_prefix(*, cwd: Path | None = None) -> str:
    """Path of `cwd` inside the repository (`services/api/`), for annotations."""
    return _git("rev-parse", "--show-prefix", cwd=cwd).strip()


def read_head(path: str, *, cwd: Path | None = None) -> str:
    root = cwd or Path()
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
