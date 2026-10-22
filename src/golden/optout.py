"""The test-presence opt-out label needs a second person.

A label the PR author applies to their own PR is a self-service bypass, and
"the system decides, not people" turns into "the author decides". So the label
counts only when someone other than the PR author applied it, and only when
that can be verified: an unverifiable label is ignored (fail closed).

Who applied it comes from the `labeled` event itself when that is the event
being run, and otherwise from the issue events API, which a `contents: read`
token can read on public repositories.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_PAGES = 10  # 1,000 issue events; a PR with more is not worth an unbounded crawl

Fetch = Callable[[str], object]


@dataclass(frozen=True)
class Decision:
    report_only: bool
    note: str  # for the job summary and an annotation; empty when the label is absent


NOT_REQUESTED = Decision(report_only=False, note="")


def api_get(path: str, token: str | None) -> object:
    """GET a GitHub REST path; None when the API is unreachable or refuses."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def applier(repository: str, number: int, label: str, fetch: Fetch) -> str | None:
    """Login of whoever most recently applied `label` to the PR; None if unknown."""
    latest: str | None = None
    for page in range(1, MAX_PAGES + 1):
        events = fetch(f"/repos/{repository}/issues/{number}/events?per_page=100&page={page}")
        if not isinstance(events, list):
            return None
        for event in events:
            if (
                isinstance(event, dict)
                and event.get("event") == "labeled"
                and (event.get("label") or {}).get("name") == label
            ):
                latest = (event.get("actor") or {}).get("login") or None
        if len(events) < 100:  # last page
            return latest
    return None  # more events than we read: the latest application may be beyond them


def decide(
    payload: dict[str, Any], event_name: str, label: str, repository: str, fetch: Fetch
) -> Decision:
    pr = payload.get("pull_request") or {}
    labels = {lbl.get("name") for lbl in pr.get("labels", []) if isinstance(lbl, dict)}
    if not label or label not in labels:
        return NOT_REQUESTED
    author = (pr.get("user") or {}).get("login")
    if event_name == "pull_request" and (payload.get("label") or {}).get("name") == label:
        who = (payload.get("sender") or {}).get("login")
    else:
        who = applier(repository, int(pr.get("number", 0)), label, fetch)
    if not who or not author:
        return Decision(
            report_only=False,
            note=f"label `{label}` ignored: could not verify who applied it, so test presence "
            "is enforced (a time-boxed `[waivers.test-presence]` is the alternative)",
        )
    if who == author:
        return Decision(
            report_only=False,
            note=f"label `{label}` ignored: the PR author (@{author}) applied it; a reviewer "
            "must apply it for test presence to become report-only",
        )
    return Decision(report_only=True, note=f"label `{label}` applied by @{who} (PR by @{author})")
