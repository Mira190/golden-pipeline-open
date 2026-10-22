"""Static-analysis findings from SARIF 2.1.0 reports, narrowed to the lines a change touched.

SARIF is the tool-neutral interchange format of CodeQL, Semgrep, Ruff (`--output-format
sarif`), golangci-lint and ESLint, so one gate serves them all.

Findings are judged the way Google's Tricorder and Meta's Infer judge them: only what the
change itself introduces, shown on the lines its author edited. A finding on untouched code is
counted, never blocking and never annotated; an offline list of old findings is the one nobody
fixes. Results suppressed in the source and results a baseline already knows are not news
either.

Fails closed like every gate: a missing, unparsable or non-SARIF report, a result without a
location, an unknown level or an invalid region is an error, never a pass.
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from golden import mutation

Range = tuple[int, int]

# SARIF result levels, weakest first. `none` carries no severity, and is what a result that is
# not a failure (kind "pass", "notApplicable", ...) gets: such results are never reported.
LEVELS = {"none": 0, "note": 1, "warning": 2, "error": 3}
# What `--min-level` accepts, strictest first.
THRESHOLDS = ("error", "warning", "note")
# SARIF's own default for a result that names no level and whose rule names none either.
DEFAULT_LEVEL = "warning"
# Findings annotated and listed in the summary; the most severe come first.
MAX_LISTED = 50
# A result a baseline comparison marks as already known, or as no longer present, is not a
# finding of this run.
IGNORED_BASELINE = frozenset({"unchanged", "absent"})
# Chain of `uriBaseId`s followed before giving up (a cycle must not hang the gate).
MAX_BASE_DEPTH = 8


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str
    message: str
    file: str
    # Inclusive line range; None for a finding about the file as a whole.
    region: Range | None = None

    @property
    def line(self) -> int | None:
        return self.region[0] if self.region else None

    def blocks(self, minimum: str) -> bool:
        return LEVELS[self.level] >= LEVELS[minimum]

    def overlaps(self, lines: dict[str, list[Range]]) -> bool:
        """On a line the change added or modified (a file-level finding: in a changed file)."""
        changed = lines.get(self.file, [])
        if self.region is None:
            return bool(changed)
        start, end = self.region
        return any(start <= hi and end >= lo for lo, hi in changed)


# ----------------------------------------------------------------- reading
def _load(path: Path) -> list[Any]:
    """The `runs` of a SARIF log. Fails closed: a missing report means the tool did not run."""
    if not path.exists():
        raise FileNotFoundError(f"SARIF report {path} not found; did the analysis tool run?")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # JSONDecodeError, and UnicodeDecodeError for a binary file
        raise ValueError(f"SARIF report {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ValueError(f"{path} is not a SARIF log: it has no `runs` list")
    runs: list[Any] = data["runs"]
    return runs


def _ignored(result: dict[str, Any]) -> bool:
    """Suppressed in the source (and the suppression accepted) or already known to a baseline."""
    if result.get("baselineState") in IGNORED_BASELINE:
        return True
    # A suppression without a status counts as accepted (Semgrep's `nosemgrep` has none).
    return any(s.get("status", "accepted") == "accepted" for s in result.get("suppressions") or [])


def _component(tool: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    """The tool component a rule reference names (an extension by index or name), else the driver.

    CodeQL keeps its query rules, and their severities, in `tool.extensions`.
    """
    driver: dict[str, Any] = tool.get("driver") or {}
    extensions: list[dict[str, Any]] = tool.get("extensions") or []
    index = ref.get("index")
    if isinstance(index, int) and 0 <= index < len(extensions):
        return extensions[index]
    name = ref.get("name")
    return next((c for c in [driver, *extensions] if name and c.get("name") == name), driver)


def _rule(run: dict[str, Any], result: dict[str, Any], rule_id: object) -> dict[str, Any]:
    """The descriptor of the rule a result refers to, or {} when the report does not carry it."""
    ref = result.get("rule") or {}
    component = _component(run.get("tool") or {}, ref.get("toolComponent") or {})
    rules: list[dict[str, Any]] = component.get("rules") or []
    index = result.get("ruleIndex", ref.get("index"))
    if isinstance(index, int) and 0 <= index < len(rules):
        return rules[index]
    return next((r for r in rules if rule_id and r.get("id") == rule_id), {})


def _level(result: dict[str, Any], rule: dict[str, Any]) -> str:
    """The result's level, else its rule's default, else "warning" (SARIF 3.27.10)."""
    level = result.get("level")
    if level is None:
        if result.get("kind", "fail") != "fail":
            return "none"
        level = (rule.get("defaultConfiguration") or {}).get("level") or DEFAULT_LEVEL
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    return str(level)


def _text(result: dict[str, Any]) -> str:
    """The message on one line: it ends up in annotations and in the job summary."""
    message = result.get("message") or {}
    text = message.get("text") or message.get("markdown") or ""
    return " ".join(str(text).split()) or "(no message)"


def _physical(result: dict[str, Any]) -> dict[str, Any]:
    """The first location that points into a file."""
    for location in result.get("locations") or []:
        physical = location.get("physicalLocation")
        if isinstance(physical, dict):
            return physical
    raise ValueError("a result has no physical location")


def _local(uri: str) -> str:
    """A SARIF URI reference as a path: `file://` dropped, percent-escapes decoded."""
    parts = urlsplit(uri)
    return unquote(parts.path if parts.scheme == "file" else uri)


def _resolve(run: dict[str, Any], uri: str, base_id: str | None, depth: int = 0) -> str:
    """`uri` joined to its `uriBaseId` (looked up in `originalUriBaseIds`) as far as it goes.

    A base the run does not define (CodeQL's `%SRCROOT%`) is the repository root: the path
    stays relative. An absolute path ignores its base, as `posixpath.join` has it.
    """
    path = _local(uri)
    if not base_id or depth >= MAX_BASE_DEPTH:
        return path
    base = (run.get("originalUriBaseIds") or {}).get(base_id) or {}
    directory = _resolve(run, base.get("uri") or "", base.get("uriBaseId"), depth + 1)
    return posixpath.join(directory, path)


def _path(run: dict[str, Any], artifact: dict[str, Any]) -> str:
    """The file, relative to the working directory like git names it."""
    uri = artifact.get("uri")
    if not isinstance(uri, str) or not uri:
        raise ValueError("a result's location has no artifact uri")
    return mutation._relative(_resolve(run, uri, artifact.get("uriBaseId")))


def _region(physical: dict[str, Any]) -> Range | None:
    """Inclusive lines of the region; None when the report only names the file."""
    region = physical.get("region") or {}
    start = region.get("startLine")
    if start is None:
        return None
    end = region.get("endLine")
    if end is None:
        end = start
    if any(isinstance(n, bool) or not isinstance(n, int) for n in (start, end)) or not (
        1 <= start <= end
    ):
        raise ValueError(f"invalid region: lines {start!r} to {end!r}")
    return start, end


def _finding(run: dict[str, Any], result: dict[str, Any]) -> Finding | None:
    if _ignored(result):
        return None
    rule_id = result.get("ruleId") or (result.get("rule") or {}).get("id")
    level = _level(result, _rule(run, result, rule_id))
    if level == "none":
        return None
    physical = _physical(result)
    return Finding(
        rule=str(rule_id or "unknown-rule"),
        level=level,
        message=_text(result),
        file=_path(run, physical.get("artifactLocation") or {}),
        region=_region(physical),
    )


def _run(run: dict[str, Any]) -> list[Finding]:
    results = run.get("results")
    if results is None:
        return []
    if not isinstance(results, list):
        raise ValueError("a run's `results` is not a list")
    return [f for r in results if (f := _finding(run, r)) is not None]


def _findings(path: Path) -> list[Finding]:
    runs = _load(path)
    try:
        return [f for run in runs for f in _run(run)]
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # Schema drift after a tool upgrade must fail closed, with a readable reason.
        raise ValueError(f"malformed SARIF report {path}: {exc!r}") from exc


def read(paths: Iterable[Path]) -> list[Finding]:
    """Every finding of every report, the same finding reported twice counted once."""
    return list(dict.fromkeys(f for path in paths for f in _findings(path)))


# ----------------------------------------------------------------- judging
def _severity(finding: Finding) -> tuple[int, str, int, str]:
    return (-LEVELS[finding.level], finding.file, finding.line or 0, finding.rule)


def narrow(findings: list[Finding], lines: dict[str, list[Range]]) -> tuple[list[Finding], int]:
    """Findings on the changed lines, most severe first, and how many lie outside the change."""
    changed = sorted((f for f in findings if f.overlaps(lines)), key=_severity)
    return changed, len(findings) - len(changed)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _bullet(finding: Finding) -> str:
    where = finding.file
    if finding.region:
        start, end = finding.region
        where += f":{start}" if start == end else f":{start}-{end}"
    # One line, whatever the report contained: a line starting with `::` would be read by the
    # runner as a workflow command.
    return " ".join(f"- `{where}` **{finding.level}** `{finding.rule}`: {finding.message}".split())


def detail(changed: list[Finding], outside: int, minimum: str) -> str:
    """The verdict's body: counts, the findings on the change, the pre-existing tally."""
    rule = f"Findings at level `{minimum}` or above block the merge."
    if changed:
        counts = Counter(f.level for f in changed)
        levels = ", ".join(f"{counts[lv]} {lv}" for lv in reversed(LEVELS) if counts[lv])
        head = f"**{_count(len(changed), 'finding')}** on changed lines ({levels}). {rule}"
    else:
        head = f"No findings on changed lines. {rule}"
    parts = [head]
    if changed:
        bullets = [_bullet(f) for f in changed[:MAX_LISTED]]
        if len(changed) > MAX_LISTED:
            bullets.append(f"- ... and {len(changed) - MAX_LISTED} more")
        parts.append("\n".join(bullets))
    if outside:
        parts.append(
            f"{_count(outside, 'pre-existing finding')} outside this change (not blocking)."
        )
    return "\n\n".join(parts)
