"""Policy-as-code: load `golden.toml`, apply defaults, enforce the platform floor.

A repository may make any gate *stricter* than the default. It may never make
one weaker than the floor; the only way below the floor is a time-boxed waiver
with a reason, which turns that gate into report-only until it expires.

A pull request is judged by the policy already merged on its base branch: it
may tighten the bar for itself, but loosening (a lower threshold, a new waiver
or exemption, a deleted file) only takes effect once the change is reviewed
and merged. A change never grades itself.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GATES = ("diff-coverage", "coverage-ratchet", "test-presence", "mutation", "sast", "scorecard")
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
    # The strictest value that can still pass; beyond it every change would fail.
    strictest: float
    higher_is_stricter: bool = True
    integer: bool = False

    def check(self, key: str, value: float) -> None:
        if not math.isfinite(value):
            raise PolicyError(f"{key} must be a finite number")
        if self.integer and not value.is_integer():
            raise PolicyError(f"{key} must be a whole number")
        weaker = value < self.floor if self.higher_is_stricter else value > self.floor
        relation = ">=" if self.higher_is_stricter else "<="
        if weaker:
            raise PolicyError(
                f"{key} = {value:g} is weaker than the platform floor ({relation} {self.floor:g})"
            )
        beyond = value > self.strictest if self.higher_is_stricter else value < self.strictest
        if beyond:
            limit = "<=" if self.higher_is_stricter else ">="
            raise PolicyError(f"{key} = {value:g} can never pass ({limit} {self.strictest:g})")

    def stricter(self, a: float, b: float) -> float:
        return max(a, b) if self.higher_is_stricter else min(a, b)


BOUNDS: dict[str, Bound] = {
    "coverage.diff_min": Bound(default=90, floor=80, strictest=100),
    "coverage.ratchet_tolerance": Bound(
        default=0.5, floor=2.0, strictest=0, higher_is_stricter=False
    ),
    "mutation.min_score": Bound(default=80, floor=60, strictest=100),
    "flaky.repeats": Bound(default=10, floor=3, strictest=100, integer=True),
    "scorecard.min_level": Bound(default=2, floor=1, strictest=4, integer=True),
}
KEYS: dict[str, set[str]] = {}
for _key in BOUNDS:
    KEYS.setdefault(_key.split(".")[0], set()).add(_key.split(".")[1])
KEYS["presence"] = {"exempt"}


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
    # Loosening this change asked for that applies only after merge.
    deferred: tuple[str, ...] = ()

    def get(self, key: str) -> float:
        return self.values[key]

    def waiver(self, gate: str, today: dt.date | None = None) -> Waiver | None:
        w = self.waivers.get(gate)
        return w if w and w.active(today or dt.datetime.now(dt.UTC).date()) else None


def _table(data: dict[str, Any], key: str, where: str = "", hint: str = "") -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise PolicyError(f"[{where}{key}] must be a table{hint}")
    return value


def _parse_waivers(raw: dict[str, Any], today: dt.date) -> dict[str, Waiver]:
    waivers: dict[str, Waiver] = {}
    for gate in raw:
        if gate not in GATES:
            raise PolicyError(f"waiver for unknown gate {gate!r}; gates are {', '.join(GATES)}")
        spec = _table(raw, gate, "waivers.", " with reason and expires")
        unknown = set(spec) - {"reason", "expires"}
        if unknown:
            raise PolicyError(f"waiver for {gate}: unknown key(s) {', '.join(sorted(unknown))}")
        reason = str(spec.get("reason", "")).strip()
        expires = spec.get("expires")
        if not reason:
            raise PolicyError(f"waiver for {gate} needs a reason")
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            raise PolicyError(f"waiver for {gate} needs `expires = YYYY-MM-DD`")
        if (expires - today).days > MAX_WAIVER_DAYS:
            raise PolicyError(f"waiver for {gate} may last at most {MAX_WAIVER_DAYS} days")
        waivers[gate] = Waiver(gate, reason, expires)
    return waivers


def from_mapping(data: dict[str, Any], today: dt.date | None = None) -> Policy:
    today = today or dt.datetime.now(dt.UTC).date()
    unknown = set(data) - set(KEYS) - {"waivers"}
    if unknown:
        raise PolicyError(f"unknown section(s): {', '.join(sorted(unknown))}")
    for section, names in KEYS.items():
        # A misspelt key would silently fall back to the default: fail closed instead.
        if typos := set(_table(data, section)) - names:
            raise PolicyError(
                f"unknown key(s) in [{section}]: {', '.join(sorted(typos))}; "
                f"known: {', '.join(sorted(names))}"
            )

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
    waivers = _parse_waivers(_table(data, "waivers"), today)
    return Policy(values, tuple(exempt), waivers)


def _parse(text: str | None, origin: str, today: dt.date | None) -> Policy:
    if text is None:
        return from_mapping({}, today)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{origin}: {exc}") from exc
    return from_mapping(data, today)


def read_at(base: str, path: Path) -> str | None:
    """The file's text at commit `base`, or None if it did not exist there."""
    spec = f"{base}:./{Path(os.path.relpath(path)).as_posix()}"
    shown = subprocess.run(["git", "show", spec], capture_output=True, text=True, check=False)
    if shown.returncode == 0:
        return shown.stdout
    commit = subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True, check=False
    )
    if commit.returncode != 0:
        raise PolicyError(f"cannot read the merged policy: {shown.stderr.strip()}")
    return None


def merge(base: Policy, head: Policy) -> Policy:
    """What a change is judged by: the merged policy, tightened but never loosened by it."""
    values = {k: b.stricter(base.values[k], head.values[k]) for k, b in BOUNDS.items()}
    deferred = [
        f"{k} = {head.values[k]:g} applies after merge (now {values[k]:g})"
        for k in BOUNDS
        if values[k] != head.values[k]
    ]
    exempt = tuple(e for e in head.exempt if e in base.exempt)
    deferred += [
        f"presence.exempt {e!r} applies after merge" for e in head.exempt if e not in exempt
    ]
    waivers: dict[str, Waiver] = {}
    for gate, proposed in head.waivers.items():
        merged = base.waivers.get(gate)
        if merged is None:
            deferred.append(f"waiver for {gate} applies after merge")
        else:
            waivers[gate] = merged if merged.expires <= proposed.expires else proposed
    return Policy(values, exempt, waivers, tuple(deferred))


def load(path: Path | None = None, today: dt.date | None = None, base: str = "") -> Policy:
    """Load `golden.toml` (missing = all defaults), anchored to commit `base` if given."""
    path = path or Path("golden.toml")
    head = _parse(path.read_text(encoding="utf-8") if path.exists() else None, str(path), today)
    if not base:
        return head
    text = read_at(base, path)
    try:
        merged = _parse(text, f"{base[:12]}:{path}", today)
    except PolicyError as exc:
        # The merged policy fails today's rules (say, a raised floor); the change
        # that repairs it must be able to pass, so judge it by its own values, which
        # meet the floors. Its own waivers and exemptions still wait for review.
        deferred = [f"merged policy ignored: {exc}"]
        deferred += [f"presence.exempt {e!r} applies after merge" for e in head.exempt]
        deferred += [f"waiver for {gate} applies after merge" for gate in head.waivers]
        return Policy(head.values, (), {}, tuple(deferred))
    return merge(merged, head)
