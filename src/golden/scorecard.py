"""Golden maturity scorecard for a consumer repository.

Levels are cumulative: a repository is at level N only when every control at
level <= N passes. Controls that need the GitHub API report "unknown" when no
token is available; unknown never counts as a pass.

  L1 Adopted   - runs the Golden Pipeline on PRs with a lockfile
  L2 Owned     - CODEOWNERS, automated dependency updates, security policy
  L3 Enforced  - the golden check is required, reviews required, history
                 protected, every action pinned, least-privilege tokens,
                 dependency cooldown
  L4 Verified  - signed provenance, OpenSSF Scorecard, no active waivers
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from golden.config import Policy

USES = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)", re.MULTILINE)
SHA_REF = re.compile(r"@[0-9a-f]{40}$")
GOLDEN_WORKFLOW = re.compile(
    r"(?:[\w.-]+/golden-pipeline(?:-open)?/|\./|\$/)\.github/workflows/golden\.yml"
)
LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "go.sum",
)


@dataclass(frozen=True)
class Repo:
    root: Path
    policy: Policy
    rules: list[dict[str, Any]] | None = None  # active branch rules from the API

    def workflows(self) -> dict[str, str]:
        wf = self.root / ".github" / "workflows"
        if not wf.is_dir():
            return {}
        files = sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")])
        return {p.name: p.read_text(encoding="utf-8") for p in files}

    def exists(self, *paths: str) -> bool:
        return any((self.root / p).exists() for p in paths)

    def rule(self, kind: str) -> dict[str, Any] | None:
        return next((r for r in self.rules or [] if r.get("type") == kind), None)


Check = Callable[[Repo], bool | None]


@dataclass(frozen=True)
class Control:
    id: str
    level: int
    title: str
    check: Check


@dataclass(frozen=True)
class Result:
    id: str
    level: int
    title: str
    passed: bool | None


def _all_text(repo: Repo) -> str:
    return "\n".join(repo.workflows().values())


def runs_on_prs(repo: Repo) -> bool:
    return re.search(r"^\s*pull_request\s*:?", _all_text(repo), re.MULTILINE) is not None


def uses_golden(repo: Repo) -> bool:
    return GOLDEN_WORKFLOW.search(_all_text(repo)) is not None


def has_lockfile(repo: Repo) -> bool:
    return any(next(repo.root.rglob(name), None) for name in LOCKFILES)


def deps_bot(repo: Repo) -> bool:
    return repo.exists(
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        "renovate.json",
        ".github/renovate.json",
        "renovate.json5",
    )


def dependency_cooldown(repo: Repo) -> bool:
    texts = [
        (repo.root / p).read_text(encoding="utf-8")
        for p in (
            ".github/dependabot.yml",
            ".github/dependabot.yaml",
            "renovate.json",
            ".github/renovate.json",
            "renovate.json5",
        )
        if (repo.root / p).exists()
    ]
    return any("cooldown" in t or "minimumReleaseAge" in t for t in texts)


def actions_pinned(repo: Repo) -> bool:
    refs = [u for u in USES.findall(_all_text(repo)) if not u.startswith(("./", "$/", "docker://"))]
    return all(SHA_REF.search(u) for u in refs)


def least_privilege(repo: Repo) -> bool:
    files = repo.workflows()
    return bool(files) and all(re.search(r"^permissions:", t, re.MULTILINE) for t in files.values())


def provenance(repo: Repo) -> bool:
    text = _all_text(repo)
    return "container.yml" in text or "actions/attest" in text


def ossf_scorecard(repo: Repo) -> bool:
    return "ossf/scorecard-action" in _all_text(repo)


def no_active_waivers(repo: Repo) -> bool:
    return not any(repo.policy.waiver(g) for g in repo.policy.waivers)


def golden_check_required(repo: Repo) -> bool | None:
    if repo.rules is None:
        return None
    rule = repo.rule("required_status_checks")
    checks = (rule or {}).get("parameters", {}).get("required_status_checks", [])
    return any(c.get("context", "").endswith("golden") for c in checks)


def reviews_required(repo: Repo) -> bool | None:
    if repo.rules is None:
        return None
    rule = repo.rule("pull_request") or {}
    return int(rule.get("parameters", {}).get("required_approving_review_count", 0)) >= 1


def history_protected(repo: Repo) -> bool | None:
    if repo.rules is None:
        return None
    return repo.rule("non_fast_forward") is not None and repo.rule("deletion") is not None


CONTROLS: tuple[Control, ...] = (
    Control("pr-ci", 1, "CI runs on pull requests", runs_on_prs),
    Control("golden", 1, "Calls a Golden Pipeline workflow", uses_golden),
    Control("lockfile", 1, "Dependencies are locked", has_lockfile),
    Control(
        "owners",
        2,
        "CODEOWNERS defines mandatory reviewers",
        lambda r: r.exists(".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"),
    ),
    Control("deps-bot", 2, "Automated dependency updates", deps_bot),
    Control(
        "security-policy",
        2,
        "SECURITY.md vulnerability disclosure policy",
        lambda r: r.exists("SECURITY.md", ".github/SECURITY.md"),
    ),
    Control("required-check", 3, "Ruleset requires the golden check", golden_check_required),
    Control("reviews", 3, "Ruleset requires an approving review", reviews_required),
    Control("history", 3, "Ruleset blocks force-push and deletion", history_protected),
    Control("pinned", 3, "Every third-party action pinned to a commit SHA", actions_pinned),
    Control("permissions", 3, "Workflows declare least-privilege permissions", least_privilege),
    Control("cooldown", 3, "Dependency updates wait out a cooldown", dependency_cooldown),
    Control("provenance", 4, "Release artifacts carry signed provenance", provenance),
    Control("ossf", 4, "OpenSSF Scorecard runs", ossf_scorecard),
    Control("waivers", 4, "No active policy waivers", no_active_waivers),
)


def fetch_rules(repository: str, branch: str, token: str | None) -> list[dict[str, Any]] | None:
    """Active rules for a branch (readable with `contents: read`); None if unavailable."""
    url = f"https://api.github.com/repos/{repository}/rules/branches/{branch}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    return data if isinstance(data, list) else None


def rules_from_env() -> list[dict[str, Any]] | None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GOLDEN_DEFAULT_BRANCH")
    if not repository or not branch:
        return None
    return fetch_rules(repository, branch, os.environ.get("GITHUB_TOKEN"))


def evaluate(repo: Repo, controls: tuple[Control, ...] = CONTROLS) -> list[Result]:
    return [Result(c.id, c.level, c.title, c.check(repo)) for c in controls]


def level(results: list[Result]) -> int:
    reached = 0
    for lvl in sorted({r.level for r in results}):
        if all(r.passed is True for r in results if r.level <= lvl):
            reached = lvl
        else:
            break
    return reached


def render(results: list[Result]) -> str:
    icon = {True: "✅", False: "❌", None: "❔ unknown"}
    rows = [f"| L{r.level} | {r.title} | {icon[r.passed]} |" for r in results]
    return "\n".join(
        [
            f"### Golden scorecard: level {level(results)} / 4",
            "",
            "| Level | Control | |",
            "|---|---|---|",
            *rows,
        ]
    )
