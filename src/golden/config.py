"""Policy-as-code: load `golden.toml`, apply defaults, enforce the platform floor.

A repository may make any gate *stricter* than the default. It may never make
one weaker than the floor; the only way below the floor is a time-boxed waiver
with a reason, which turns that gate into report-only until it expires.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GATES = ("diff-coverage", "coverage-ratchet", "test-presence", "mutation", "scorecard")
MAX_WAIVER_DAYS = 90
# An exemption that names no directory or file stem ("**", "*.py", "**/*")
# switches test presence and mutation off for a whole language: that is a
# waiver without a reason or expiry, so it is rejected.
BLANKET_EXEMPT = re.compile(r"^(\*\*/)*\*{1,2}(\.[^/*]+)?$")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Bound:
    default: float
    floor: float
    higher_is_stricter: bool = True

    def check(self, key: str, value: float) -> None:
        if not math.isfinite(value):
            raise PolicyError(f"{key} must be a finite number")
        weaker = value < self.floor if self.higher_is_stricter else value > self.floor
        if weaker:
            relation = ">=" if self.higher_is_stricter else "<="
            raise PolicyError(
                f"{key} = {value:g} is weaker than the platform floor ({relation} {self.floor:g})"
            )


BOUNDS: dict[str, Bound] = {
    "coverage.diff_min": Bound(default=90, floor=80),
    "coverage.ratchet_tolerance": Bound(default=0.5, floor=2.0, higher_is_stricter=False),
    "mutation.min_score": Bound(default=80, floor=60),
    "flaky.repeats": Bound(default=10, floor=3),
    "scorecard.min_level": Bound(default=2, floor=1),
}


@dataclass(frozen=True)
class Waiver:
    gate: str
    reason: str
    expires: dt.date

    def active(self, today: dt.date) -> bool:
        return today <= self.expires


@dataclass(frozen=True)
class Policy:
    values: dict[str, float]
    exempt: tuple[str, ...] = ()
    waivers: dict[str, Waiver] = field(default_factory=dict)

    def get(self, key: str) -> float:
        return self.values[key]

    def waiver(self, gate: str, today: dt.date | None = None) -> Waiver | None:
        w = self.waivers.get(gate)
        return w if w and w.active(today or dt.datetime.now(dt.UTC).date()) else None


def _parse_waivers(raw: dict[str, Any], today: dt.date) -> dict[str, Waiver]:
    waivers: dict[str, Waiver] = {}
    for gate, spec in raw.items():
        if gate not in GATES:
            raise PolicyError(f"waiver for unknown gate {gate!r}; gates are {', '.join(GATES)}")
        if not isinstance(spec, dict):
            raise PolicyError(f"[waivers.{gate}] must be a table with reason and expires")
        reason = str(spec.get("reason", "")).strip()
        expires = spec.get("expires")
        if not reason:
            raise PolicyError(f"waiver for {gate} needs a reason")
        # TOML datetimes are dates too (dt.datetime subclasses dt.date).
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            raise PolicyError(f"waiver for {gate} needs `expires = YYYY-MM-DD`")
        if (expires - today).days > MAX_WAIVER_DAYS:
            raise PolicyError(f"waiver for {gate} may last at most {MAX_WAIVER_DAYS} days")
        waivers[gate] = Waiver(gate, reason, expires)
    return waivers


def _check_shape(data: dict[str, Any]) -> None:
    """Every section is a table and every key is known: a typo must not keep a default."""
    known_sections = {k.split(".")[0] for k in BOUNDS} | {"presence", "waivers"}
    unknown = set(data) - known_sections
    if unknown:
        raise PolicyError(f"unknown section(s): {', '.join(sorted(unknown))}")
    known_keys: dict[str, set[str]] = {"presence": {"exempt"}}
    for key in BOUNDS:
        section, name = key.split(".")
        known_keys.setdefault(section, set()).add(name)
    for section, keys in known_keys.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise PolicyError(f"[{section}] must be a table")
        if typos := set(table) - keys:
            raise PolicyError(f"unknown key(s) in [{section}]: {', '.join(sorted(typos))}")
    if not isinstance(data.get("waivers", {}), dict):
        raise PolicyError("[waivers] must be a table")


def from_mapping(data: dict[str, Any], today: dt.date | None = None) -> Policy:
    today = today or dt.datetime.now(dt.UTC).date()
    _check_shape(data)
    values: dict[str, float] = {}
    for key, bound in BOUNDS.items():
        section, name = key.split(".")
        value = data.get(section, {}).get(name, bound.default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise PolicyError(f"{key} must be a number")
        bound.check(key, float(value))
        values[key] = float(value)

    exempt = data.get("presence", {}).get("exempt", [])
    if not isinstance(exempt, list) or not all(isinstance(e, str) for e in exempt):
        raise PolicyError("presence.exempt must be a list of glob strings")
    if blanket := [e for e in exempt if BLANKET_EXEMPT.match(e.strip().lstrip("/"))]:
        raise PolicyError(
            f"presence.exempt {blanket} would exempt every file; name the directories or files, "
            "or use a time-boxed [waivers.test-presence]"
        )
    return Policy(values, tuple(exempt), _parse_waivers(data.get("waivers", {}), today))


def load(path: Path | None = None, today: dt.date | None = None) -> Policy:
    """Load `golden.toml`; a missing file means "all defaults"."""
    path = path or Path("golden.toml")
    if not path.exists():
        return from_mapping({}, today)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{path}: {exc}") from exc
    return from_mapping(data, today)
