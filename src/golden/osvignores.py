"""Exceptions to the vulnerability gate follow the waiver rules.

OSV-Scanner reads `[[IgnoredVulns]]` from `osv-scanner.toml` next to the
lockfiles it scans. An entry without an expiry would silence an advisory
forever, which makes it a permanent, unreviewed bypass. Every entry must
name the advisory, give a reason and expire within MAX_WAIVER_DAYS, exactly
like a `golden.toml` waiver.
"""

from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass
from pathlib import Path

from golden.config import MAX_WAIVER_DAYS, PolicyError

SKIP_DIRS = {"node_modules", ".git", ".venv", "vendor", "mutants"}


@dataclass(frozen=True)
class Ignore:
    file: str
    id: str
    reason: str
    until: dt.date


def files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("osv-scanner.toml")
        if not SKIP_DIRS.intersection(p.relative_to(root).parts)
    )


def _text(value: object) -> str:
    """The text of a TOML string, stripped; empty when the value is no string."""
    return value.strip() if isinstance(value, str) else ""


def _until(value: object, where: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise PolicyError(f"{where}: ignoreUntil is required (a TOML date, e.g. 2026-12-31)")


def check(root: Path, today: dt.date) -> list[Ignore]:
    """Every exception in every osv-scanner.toml under `root`; raises on a bad one."""
    found: list[Ignore] = []
    for path in files(root):
        name = path.relative_to(root).as_posix()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            # TOMLDecodeError, not UTF-8, an integer of more than 4300 digits, or arrays
            # nested too deeply.
            raise PolicyError(f"{name}: {exc}") from exc
        entries = data.get("IgnoredVulns", [])
        if not isinstance(entries, list):
            raise PolicyError(f"{name}: IgnoredVulns must be an array of tables")
        for index, entry in enumerate(entries):
            where = f"{name}: IgnoredVulns[{index}]"
            # A number or a list is no advisory id and no reason: str() would accept both.
            if not isinstance(entry, dict) or not _text(entry.get("id")):
                raise PolicyError(f"{where}: id is required")
            reason = _text(entry.get("reason"))
            if not reason:
                raise PolicyError(f"{where} ({entry['id']}): reason is required")
            until = _until(entry.get("ignoreUntil"), f"{where} ({entry['id']})")
            if (until - today).days > MAX_WAIVER_DAYS:
                raise PolicyError(
                    f"{where} ({entry['id']}): ignoreUntil may be at most "
                    f"{MAX_WAIVER_DAYS} days ahead"
                )
            found.append(Ignore(name, entry["id"], reason, until))
    return found
