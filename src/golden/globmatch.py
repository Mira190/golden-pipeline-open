"""Gitignore-style glob matching for repository-relative POSIX paths.

Supports `**` (any number of directories), `*` and `?` (never crossing `/`),
and `{a,b}` alternation. Patterns without a `/` match at any depth, like
`.gitignore`.
"""

from __future__ import annotations

import re
from functools import lru_cache


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{":
            end = pattern.find("}", i)
            if end == -1:
                out.append(re.escape(c))
            else:
                options = pattern[i + 1 : end].split(",")
                out.append("(?:" + "|".join(_translate(o) for o in options) + ")")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=512)
def compile_glob(pattern: str) -> re.Pattern[str]:
    if "/" not in pattern.rstrip("/"):
        pattern = "**/" + pattern
    pattern = pattern.removeprefix("./")
    if pattern.endswith("/"):
        pattern += "**"
    return re.compile(_translate(pattern) + r"\Z")


def match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).match(path) is not None


def match_any(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(match(path, p) for p in patterns)
