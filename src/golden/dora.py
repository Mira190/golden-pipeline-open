"""The four DORA keys from a deployment log (JSON Lines).

Each record: {"sha": str, "committed_at": ISO-8601, "deployed_at": ISO-8601,
              "failed": bool, "restored_at": ISO-8601 | null}
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Dora:
    deploys_per_day: float
    lead_time_hours_p50: float
    change_failure_rate: float
    time_to_restore_hours_p50: float | None


def _hours(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600


def compute(records: list[dict[str, Any]]) -> Dora:
    if not records:
        raise ValueError("no deployments recorded")
    deployed = sorted(datetime.fromisoformat(r["deployed_at"]) for r in records)
    span_days = max((deployed[-1] - deployed[0]).total_seconds() / 86400, 1.0)
    failures = [r for r in records if r["failed"]]
    restore = [_hours(r["deployed_at"], r["restored_at"]) for r in failures if r.get("restored_at")]
    return Dora(
        deploys_per_day=round(len(records) / span_days, 2),
        lead_time_hours_p50=round(
            statistics.median(_hours(r["committed_at"], r["deployed_at"]) for r in records), 2
        ),
        change_failure_rate=round(len(failures) / len(records), 3),
        time_to_restore_hours_p50=round(statistics.median(restore), 2) if restore else None,
    )
