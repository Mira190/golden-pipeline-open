"""What changed on this branch, relative to the current directory."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
Range = tuple[int, int]
# Pin the output format: a user's `diff.noprefix` or `core.quotePath` setting
# must not change what the parser sees.
DIFF_OPTS = ("-c", "core.quotePath=false")
PREFIXES = ("--src-prefix=a/", "--dst-prefix=b/")
C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


class GitError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    # Bytes, not text mode: universal newlines would turn a lone "\r" inside a
    # changed line into a line break and throw off the hunk line counts.
    try:
        out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(err or str(exc)) from exc
    return out.decode("utf-8", errors="surrogateescape")


def changed_files(base: str, *, cwd: Path | None = None) -> list[str]:
    """Files added/copied/modified/renamed since merge-base(base, HEAD).

    Paths are relative to `cwd` and limited to files beneath it, so the gates
    work for a project living in a subdirectory of a monorepo.
    """
    out = _git(
        *DIFF_OPTS,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=ACMR",
        "--relative",
        f"{base}...HEAD",
        cwd=cwd,
    )
    return sorted(name for name in out.split("\0") if name)


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of unusual file names ("a\\tb.py", "caf\\303\\251.py")."""
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    raw, body, i = bytearray(), path[1:-1], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                raw.append(int(body[i + 1 : i + 4], 8))
                i += 4
                continue
            raw.append(C_ESCAPES.get(nxt, ord(nxt)))
            i += 2
            continue
        raw.extend(body[i].encode())
        i += 1
    return raw.decode("utf-8", errors="surrogateescape")


def _target(header: str) -> str:
    # git appends a TAB to `+++ b/name` when the name contains a space.
    return _unquote(header[4:].removesuffix("\t")).removeprefix("b/")


def changed_lines(base: str, *, cwd: Path | None = None) -> dict[str, list[Range]]:
    """Changed (added or modified) line ranges per file, inclusive, in HEAD's numbering."""
    out = _git(
        *DIFF_OPTS,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        *PREFIXES,
        "--diff-filter=ACMR",
        "--relative",
        f"{base}...HEAD",
        cwd=cwd,
    )
    result: dict[str, list[Range]] = {}
    current: str | None = None
    # Hunk bodies are skipped by count, so an added line that itself starts
    # with "++ " (shown as "+++ ...") can never be mistaken for a file header.
    body = 0
    for line in out.split("\n"):
        if body:
            body -= not line.startswith("\\")  # "\ No newline at end of file"
        elif line.startswith("+++ "):
            current = _target(line)
        elif current and (m := HUNK.match(line)):
            old = int(m[1]) if m[1] is not None else 1
            start, count = int(m[2]), int(m[3]) if m[3] is not None else 1
            body = old + count
            if count:
                result.setdefault(current, []).append((start, start + count - 1))
    return result


def merge_base(ref: str, *, cwd: Path | None = None) -> str:
    return _git("merge-base", "HEAD", ref, cwd=cwd).strip()


def uncommitted(*, cwd: Path | None = None) -> bool:
    """Work not yet committed, new files included (the diff gates only see `base...HEAD`)."""
    return bool(_git("status", "--porcelain", ".", cwd=cwd).strip())


def repo_prefix(*, cwd: Path | None = None) -> str:
    """Path of `cwd` inside the repository (`services/api/`), for annotations."""
    return _git("rev-parse", "--show-prefix", cwd=cwd).strip()


def read_head(path: str, *, cwd: Path | None = None) -> str:
    root = cwd or Path()
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
