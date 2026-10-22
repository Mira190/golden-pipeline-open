"""Golden maturity scorecard for a consumer repository.

Levels are cumulative: a repository is at level N only when every control at
level <= N passes. Controls that need the GitHub API report "unknown" when no
token is available; unknown never counts as a pass.

  L1 Adopted   - runs the Golden Pipeline on PRs with a lockfile
  L2 Owned     - CODEOWNERS, automated dependency updates, security policy
  L3 Enforced  - the golden check is required, reviews required, code owners
                 approve workflow and policy changes, history protected, every
                 action pinned, least-privilege tokens, no gate switched off in
                 the caller, dependency cooldown
  L4 Verified  - signed provenance, OpenSSF Scorecard, no active waivers, a second
                 person approves the final code (two-person review), a merge queue
                 keeps the default branch green, static analysis (CodeQL or Semgrep,
                 or a code scanning rule) gates pull requests, release tags can't be
                 moved or deleted

Checks read workflow files with YAML comments removed, so a commented-out line
never counts as a control.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from golden.config import Policy
from golden.globmatch import match

USES = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)", re.MULTILINE)
# The `pull_request` trigger, not `pull_request_target` or `github.event.pull_request`.
PR_TRIGGER = re.compile(r"(?<![\w.'\"-])pull_request(?![\w-])")
YAML_COMMENT = re.compile(r"(^|\s)#.*$", re.MULTILINE)
# `<input>: false` in a caller's `with:` block.
SWITCHED_OFF = "^\\s*{}\\s*:\\s*false\\b"
DEPS_BOT_FILES = (
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
    "renovate.json",
    "renovate.json5",
    ".github/renovate.json",
    ".github/renovate.json5",
    ".gitlab/renovate.json",
    ".renovaterc",
    ".renovaterc.json",
    ".renovaterc.json5",
)
CODEOWNERS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
# Directories that hold other projects' files, never this project's lockfile.
FOREIGN_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "mutants", ".stryker-tmp"}
SHA_REF = re.compile(r"@[0-9a-f]{40}$")
API = "https://api.github.com"
# Static analysis that reports findings on the diff: CodeQL's analyze step, Semgrep's actions.
SAST_USES = (
    "github/codeql-action/analyze@",
    "returntocorp/semgrep-action@",
    "semgrep/semgrep-action@",
)
# A `semgrep ` command (not `pip install semgrep` or `echo semgrep`) in a shell script.
SEMGREP_RUN = re.compile(
    r"""(?:^|[;&|(])\s*["']?(?:(?:sudo|uvx|pipx\s+run|uv\s+run)\s+)?semgrep\s""", re.MULTILINE
)
RUN_KEY = re.compile(r"^(?P<indent>[ \t]*(?:-[ \t]+)?)run:[ \t]*(?P<value>.*)$")
# Ref conditions of a tag ruleset that cover version tags, and a tag the exclusions must not hit.
RELEASE_TAG_REFS = frozenset({"~ALL", "refs/tags/v*", "refs/tags/*", "refs/tags/**"})
PROBE_TAG = "refs/tags/v1.0.0"
GOLDEN_WORKFLOW = re.compile(
    r"(?:[\w.-]+/golden-pipeline(?:-open)?/|\./|\$/)\.github/workflows/golden\.yml"
)
# A call of this repository's sast.yml, which runs CodeQL and gates its findings on the diff.
GOLDEN_SAST = re.compile(
    r"^(?:[\w.-]+/golden-pipeline(?:-open)?/|\./|\$/)\.github/workflows/sast\.yml(?:@|$)"
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
    policy_path: str = "golden.toml"  # relative to root
    tag_rulesets: list[dict[str, Any]] | None = None  # every tag ruleset, in full, from the API

    def workflows(self) -> dict[str, str]:
        """Workflow files with YAML comments removed."""
        wf = self.root / ".github" / "workflows"
        if not wf.is_dir():
            return {}
        files = sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")])
        return {p.name: YAML_COMMENT.sub(r"\1", p.read_text(encoding="utf-8")) for p in files}

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


def _uses(repo: Repo) -> list[str]:
    """Every `uses:` reference; a mention in a comment or string satisfies no control."""
    return USES.findall(_all_text(repo))


def runs_on_prs(repo: Repo) -> bool:
    return PR_TRIGGER.search(_all_text(repo)) is not None


def uses_golden(repo: Repo) -> bool:
    return any(GOLDEN_WORKFLOW.search(u) for u in _uses(repo))


def has_lockfile(repo: Repo) -> bool:
    for _, dirs, files in os.walk(repo.root):
        dirs[:] = [d for d in dirs if d not in FOREIGN_DIRS]
        if any(name in files for name in LOCKFILES):
            return True
    return False


def deps_bot(repo: Repo) -> bool:
    return repo.exists(*DEPS_BOT_FILES)


def dependency_cooldown(repo: Repo) -> bool:
    for name in DEPS_BOT_FILES:
        path = repo.root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "dependabot" in name:
            if re.search(r"^\s*(?:-\s*)?cooldown\s*:", YAML_COMMENT.sub(r"\1", text), re.M):
                return True
        elif re.search(r"""^[^/\n]*["']?minimumReleaseAge["']?\s*:""", text, re.MULTILINE):
            return True
    return False


def actions_pinned(repo: Repo) -> bool:
    refs = [u for u in _uses(repo) if not u.startswith(("./", "$/", "docker://"))]
    return all(SHA_REF.search(u) for u in refs)


def top_level_permissions(text: str) -> str:
    """The workflow-level `permissions:` value (inline or block), or "" if undeclared."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if m := re.match(r"permissions:(.*)$", line):
            if m[1].strip():
                return m[1].strip()
            block = []
            for nxt in lines[i + 1 :]:
                if nxt.strip() and not nxt[0].isspace():
                    break
                block.append(nxt.strip())
            return "\n".join(b for b in block if b)
    return ""


def least_privilege(repo: Repo) -> bool:
    """Every workflow defaults to a read-only token; write scopes go on single jobs."""
    files = repo.workflows()
    tops = [top_level_permissions(t) for t in files.values()]
    return bool(files) and all(
        top and top != "write-all" and not re.search(r":\s*write\b", top) for top in tops
    )


def gates_enabled(repo: Repo) -> bool:
    """Callers may not switch gates off; a time-boxed waiver in golden.toml is the way.

    Mutation testing is per project, so no caller may skip it. The security scans cover the
    whole repository, so one caller running them is enough (a monorepo scans once).
    """
    callers = "\n".join(t for t in repo.workflows().values() if GOLDEN_WORKFLOW.search(t))
    calls = len(GOLDEN_WORKFLOW.findall(callers))

    def off(name: str) -> int:
        return len(re.findall(SWITCHED_OFF.format(re.escape(name)), callers, re.MULTILINE))

    if not calls:
        return True  # nothing to switch off; "Calls a Golden Pipeline workflow" fails instead
    return off("mutation") == 0 and off("security") < calls and off("workflow-lint") < calls


def provenance(repo: Repo) -> bool:
    return any(
        u.startswith("actions/attest")
        or u.split("@")[0].endswith("/.github/workflows/container.yml")
        for u in _uses(repo)
    )


def ossf_scorecard(repo: Repo) -> bool:
    return any(u.startswith("ossf/scorecard-action@") for u in _uses(repo))


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


def owned(repo: Repo, path: str) -> bool:
    """CODEOWNERS assigns `path` to someone (the last matching pattern wins)."""
    file = next((repo.root / c for c in CODEOWNERS if (repo.root / c).exists()), None)
    owners: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines() if file else []:
        pattern, *who = line.split("#", 1)[0].split() or [""]
        if pattern and match(path, "./" + pattern[1:] if pattern.startswith("/") else pattern):
            owners = who
    return bool(owners)


def owner_review(repo: Repo) -> bool | None:
    """Changes to workflows and golden.toml need a code owner, so no PR weakens its own gate."""
    if repo.rules is None:
        return None
    rule = repo.rule("pull_request") or {}
    if not rule.get("parameters", {}).get("require_code_owner_review"):
        return False
    return owned(repo, ".github/workflows/ci.yml") and owned(repo, repo.policy_path)


def history_protected(repo: Repo) -> bool | None:
    if repo.rules is None:
        return None
    return repo.rule("non_fast_forward") is not None and repo.rule("deletion") is not None


def fresh_review(repo: Repo) -> bool | None:
    """A second person approves the final code: a new push dismisses earlier approvals, and
    whoever pushed last cannot approve their own push. A missing rule or parameter fails."""
    if repo.rules is None:
        return None
    params = (repo.rule("pull_request") or {}).get("parameters") or {}
    return (
        params.get("dismiss_stale_reviews_on_push") is True
        and params.get("require_last_push_approval") is True
    )


def merge_queue(repo: Repo) -> bool | None:
    """The default branch merges through a merge queue, which tests each pull request on top of
    the queued ones, so concurrent merges can't break the branch (rule type `merge_queue`)."""
    if repo.rules is None:
        return None
    return repo.rule("merge_queue") is not None


def run_scripts(text: str) -> list[str]:
    """The shell of every `run:` step: the inline value plus a block scalar's indented lines."""
    lines = text.splitlines()
    scripts = []
    for i, line in enumerate(lines):
        if not (m := RUN_KEY.match(line)):
            continue
        body = [m["value"]]
        for nxt in lines[i + 1 :]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= len(m["indent"]):
                break
            body.append(nxt)
        scripts.append("\n".join(body))
    return scripts


def runs_sast_on_prs(text: str) -> bool:
    """One workflow file (comments removed) runs static analysis on pull requests: a CodeQL
    analyze or Semgrep action step, a call of golden's sast.yml, or a `semgrep` command in a
    `run:` script."""
    return PR_TRIGGER.search(text) is not None and (
        any(u.startswith(SAST_USES) or GOLDEN_SAST.match(u) for u in USES.findall(text))
        or any(SEMGREP_RUN.search(script) for script in run_scripts(text))
    )


def code_scanning(repo: Repo) -> bool | None:
    """Static analysis reports on the diff: a workflow runs it on pull requests (evidence enough
    without the API), or an active `code_scanning` rule requires results from at least one tool."""
    if any(runs_sast_on_prs(text) for text in repo.workflows().values()):
        return True
    if repo.rules is None:
        return None
    tools = ((repo.rule("code_scanning") or {}).get("parameters") or {}).get("code_scanning_tools")
    return isinstance(tools, list) and len(tools) > 0


def locks_release_tags(ruleset: dict[str, Any]) -> bool:
    """An active tag ruleset that blocks deleting and moving version tags: a `deletion` rule plus
    `non_fast_forward` or `update`, on `~ALL` or `refs/tags/v*`, not excluding `v1.0.0`."""
    if ruleset.get("target") != "tag" or ruleset.get("enforcement") != "active":
        return False
    ref_name = (ruleset.get("conditions") or {}).get("ref_name") or {}
    types = {r.get("type") for r in ruleset.get("rules") or []}
    return (
        "deletion" in types
        and bool(types & {"non_fast_forward", "update"})
        and not RELEASE_TAG_REFS.isdisjoint(ref_name.get("include") or [])
        and not any(p == "~ALL" or fnmatchcase(PROBE_TAG, p) for p in ref_name.get("exclude") or [])
    )


def release_tags(repo: Repo) -> bool | None:
    if repo.tag_rulesets is None:
        return None
    return any(locks_release_tags(r) for r in repo.tag_rulesets)


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
    Control(
        "owner-review", 3, "Code owners must approve workflow and policy changes", owner_review
    ),
    Control("history", 3, "Ruleset blocks force-push and deletion", history_protected),
    Control("pinned", 3, "Every third-party action pinned to a commit SHA", actions_pinned),
    Control("permissions", 3, "Workflows declare least-privilege permissions", least_privilege),
    Control("gates-on", 3, "No gate switched off in the caller workflow", gates_enabled),
    Control("cooldown", 3, "Dependency updates wait out a cooldown", dependency_cooldown),
    Control("provenance", 4, "Release artifacts carry signed provenance", provenance),
    Control("ossf", 4, "OpenSSF Scorecard runs", ossf_scorecard),
    Control("waivers", 4, "No active policy waivers", no_active_waivers),
    Control(
        "fresh-review",
        4,
        "Approvals are dismissed on new pushes and the last pusher cannot self-approve",
        fresh_review,
    ),
    Control("merge-queue", 4, "Merge queue keeps the default branch green", merge_queue),
    Control("code-scanning", 4, "Static analysis runs on pull requests", code_scanning),
    Control("release-tags", 4, "Release tags can't be moved or deleted", release_tags),
)


def _get_json(url: str, token: str | None) -> object:
    """GET a GitHub API URL (10 s timeout) and parse the JSON; None if that fails in any way."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def fetch_rules(repository: str, branch: str, token: str | None) -> list[dict[str, Any]] | None:
    """Active rules for a branch (readable with `contents: read`); None if unavailable."""
    data = _get_json(f"{API}/repos/{repository}/rules/branches/{branch}", token)
    return data if isinstance(data, list) else None


def rules_from_env() -> list[dict[str, Any]] | None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GOLDEN_DEFAULT_BRANCH")
    if not repository or not branch:
        return None
    return fetch_rules(repository, branch, os.environ.get("GITHUB_TOKEN"))


def fetch_tag_rulesets(repository: str, token: str | None) -> list[dict[str, Any]] | None:
    """Every tag ruleset that applies to the repository, in full; None if any call fails.

    The list endpoint returns summaries (name, enforcement, links), so the `conditions` and
    `rules` come from one GET per ruleset. Both need only `Metadata: read`, which every
    `GITHUB_TOKEN` has; organisation rulesets are included (`includes_parents`).
    """
    base = f"{API}/repos/{repository}/rulesets"
    listing = _get_json(f"{base}?includes_parents=true&targets=tag&per_page=100", token)
    if not isinstance(listing, list):
        return None
    rulesets = []
    for summary in listing:
        ruleset_id = summary.get("id") if isinstance(summary, dict) else None
        if not isinstance(ruleset_id, int):
            return None
        ruleset = _get_json(f"{base}/{ruleset_id}?includes_parents=true", token)
        if not isinstance(ruleset, dict):
            return None
        rulesets.append(ruleset)
    return rulesets


def tag_rulesets_from_env() -> list[dict[str, Any]] | None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        return None
    return fetch_tag_rulesets(repository, os.environ.get("GITHUB_TOKEN"))


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
