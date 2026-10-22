"""DORA software-delivery metrics from a deployment log (JSON Lines).

Each record: {"sha": str, "committed_at": ISO-8601, "deployed_at": ISO-8601,
              "failed": bool, "restored_at": ISO-8601 | null,
              "rework": bool (optional: an unplanned deployment fixing a production incident)}

Timestamps must carry a UTC offset. Deployment rework rate (added by DORA in 2024)
is reported only when the log records `rework`.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

REQUIRED = ("committed_at", "deployed_at", "failed")
TIMESTAMPS = ("committed_at", "deployed_at", "restored_at")


@dataclass(frozen=True)
class Dora:
    deploys_per_day: float
    lead_time_hours_p50: float
    change_failure_rate: float
    time_to_restore_hours_p50: float | None
    deployment_rework_rate: float | None = None


def _when(record: dict[str, Any], key: str, where: str) -> datetime:
    try:
        moment = datetime.fromisoformat(record[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where}: {key} is not an ISO-8601 timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError(f"{where}: {key} needs a UTC offset (e.g. +00:00)")
    return moment


def parse_log(text: str) -> list[dict[str, Any]]:
    """Validated records from a JSON Lines log; errors name the offending line."""
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"line {number}"
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{where}: not JSON ({exc})") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{where}: expected a JSON object")
        missing = [k for k in REQUIRED if k not in record]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        times = {k: _when(record, k, where) for k in TIMESTAMPS if record.get(k) is not None}
        if times["deployed_at"] < times["committed_at"]:
            raise ValueError(f"{where}: deployed_at is before committed_at")
        if "restored_at" in times and times["restored_at"] < times["deployed_at"]:
            raise ValueError(f"{where}: restored_at is before deployed_at")
        records.append(record)
    return records


def _hours(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600


def compute(records: list[dict[str, Any]]) -> Dora:
    if not records:
        raise ValueError("no deployments recorded")
    deployed = sorted(datetime.fromisoformat(r["deployed_at"]) for r in records)
    span_days = max((deployed[-1] - deployed[0]).total_seconds() / 86400, 1.0)
    failures = [r for r in records if r["failed"]]
    restore = [_hours(r["deployed_at"], r["restored_at"]) for r in failures if r.get("restored_at")]
    rework = [bool(r["rework"]) for r in records if "rework" in r]
    return Dora(
        deploys_per_day=round(len(records) / span_days, 2),
        lead_time_hours_p50=round(
            statistics.median(_hours(r["committed_at"], r["deployed_at"]) for r in records), 2
        ),
        change_failure_rate=round(len(failures) / len(records), 3),
        time_to_restore_hours_p50=round(statistics.median(restore), 2) if restore else None,
        deployment_rework_rate=round(sum(rework) / len(rework), 3) if rework else None,
    )
