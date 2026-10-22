"""Plan a pipeline run: effective commands, diff base, and what changed.

The reusable workflow passes its inputs as JSON; empty inputs fall back to the
language defaults below. Keeping the defaults here (not in YAML) makes them
versioned, reviewable and unit-tested like any other code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from golden import gitdiff, presets
from golden.config import Policy
from golden.globmatch import match_any

ZERO_SHA = "0" * 40

PY_TEST_PLUGINS = (
    "--with pytest-cov==7.1.0 --with pytest-xdist==3.8.0 --with pytest-randomly==5.0.0"
)
MUTMUT = "uv run --with mutmut==3.8.0 mutmut"
GO_TOOLS = {
    "cobertura": "github.com/boumenot/gocover-cobertura@v1.5.0",
    "govulncheck": "golang.org/x/vuln/cmd/govulncheck@v1.9.0",
    "gremlins": "github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0",
}

DEFAULTS: dict[str, dict[str, str]] = {
    "python": {
        "versions": '["3.12", "3.13"]',
        "install-command": "uv sync --locked",
        "format-command": "uvx ruff@0.17.0 format --check --diff .",
        "lint-command": "uvx ruff@0.17.0 check --output-format=github .",
        "typecheck-command": "uv run --with mypy==2.4.0 mypy .",
        "test-command": f"uv run {PY_TEST_PLUGINS} pytest -n auto -p randomly "
        "--cov --cov-report=xml:coverage.xml --cov-report=term",
        "coverage-report": "coverage.xml",
        "flaky-command": f"uv run {PY_TEST_PLUGINS} --with pytest-repeat==0.9.4 pytest -n auto "
        '-p randomly --count="$GOLDEN_REPEATS" $GOLDEN_TESTS',
        "mutation-command": f"{MUTMUT} run $GOLDEN_MUTATE && {MUTMUT} export-cicd-stats",
        "mutation-report": "mutants/mutmut-cicd-stats.json",
        "mutation-format": "mutmut",
    },
    "node": {
        "versions": '["22", "24"]',
        "install-command": "",  # chosen from the lockfile, see _node_install
        "format-command": "",  # `biome ci` checks formatting as part of lint
        "lint-command": "npx --no-install biome ci .",
        "typecheck-command": "npx --no-install tsc --noEmit",
        "test-command": "npx --no-install vitest run --coverage.enabled "
        "--coverage.reporter=cobertura --coverage.reporter=text --sequence.shuffle",
        "coverage-report": "coverage/cobertura-coverage.xml",
        # A fresh process and shuffle seed per run; works for Vitest 4/5 and Jest alike.
        "flaky-command": 'for i in $(seq "$GOLDEN_REPEATS"); do '
        "npx --no-install vitest run --sequence.shuffle $GOLDEN_TESTS || exit 1; done",
        "mutation-command": 'npx --no-install stryker run --mutate "$GOLDEN_MUTATE" '
        "--reporters json,clear-text",
        "mutation-report": "reports/mutation/mutation.json",
        "mutation-format": "stryker",
    },
    "go": {
        "versions": '["go.mod"]',
        "install-command": "go mod download",
        "format-command": 'test -z "$(gofmt -l .)" || { gofmt -d .; exit 1; }',
        "lint-command": f"go vet ./... && go run {GO_TOOLS['govulncheck']} ./...",
        "typecheck-command": "go build ./...",
        "test-command": "go test -race -shuffle=on -covermode=atomic -coverprofile=coverage.out "
        f"./... && go run {GO_TOOLS['cobertura']} -ignore-gen-files < coverage.out > coverage.xml",
        "coverage-report": "coverage.xml",
        "flaky-command": 'go test -race -shuffle=on -count="$GOLDEN_REPEATS" $GOLDEN_TEST_PACKAGES',
        # Whole module, narrowed to changed lines by `golden mutation --base`:
        # gremlins' own --diff misses files when the module is in a subdirectory.
        "mutation-command": f"go run {GO_TOOLS['gremlins']} unleash --timeout-coefficient 10 "
        "--output gremlins.json .",
        "mutation-report": "gremlins.json",
        "mutation-format": "gremlins",
    },
}
MUTATE_SEPARATOR = {"python": " ", "node": ",", "go": " "}


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any]

    @classmethod
    def from_env(cls) -> Event:
        path = os.environ.get("GITHUB_EVENT_PATH")
        payload = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        return cls(os.environ.get("GITHUB_EVENT_NAME", ""), payload)

    def base(self) -> str:
        """The commit a change is judged against; empty when there is no change to judge."""
        if self.name in {"pull_request", "pull_request_target"}:
            return str(self.payload["pull_request"]["base"]["sha"])
        if self.name == "merge_group":
            return str(self.payload["merge_group"]["base_sha"])
        return ""

    def labels(self) -> set[str]:
        return {lbl["name"] for lbl in self.payload.get("pull_request", {}).get("labels", [])}


def _node_install(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm install --frozen-lockfile --ignore-scripts"
    if (root / "yarn.lock").exists():
        return "yarn install --immutable --mode=skip-build"
    if (root / "bun.lock").exists():
        return "bun install --frozen-lockfile --ignore-scripts"
    return "npm ci --ignore-scripts"


def _node_package_manager(root: Path) -> str:
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
    ):
        if (root / lockfile).exists():
            return manager
    return "npm"


def commands(language: str, inputs: dict[str, Any], root: Path) -> dict[str, str]:
    if language not in DEFAULTS:
        raise ValueError(f"unsupported language {language!r}; choose from {', '.join(DEFAULTS)}")
    effective = dict(DEFAULTS[language])
    if language == "node":
        effective["install-command"] = _node_install(root)
    for key in effective:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            effective[key] = value.strip()
    versions = json.loads(effective["versions"])
    if not isinstance(versions, list) or not versions:
        raise ValueError("versions must be a non-empty JSON list")
    effective["primary-version"] = str(versions[-1])
    effective["coverage-dir"] = str(PurePosixPath(effective["coverage-report"]).parent)
    effective["package-manager"] = _node_package_manager(root) if language == "node" else ""
    return effective


def changes(language: str, base: str, policy: Policy) -> dict[str, str]:
    """Changed tests, test packages and mutation targets relative to `base`."""
    if not base:
        return {"tests": "", "test-packages": "", "mutate": "", "sources-changed": "false"}
    preset = presets.get(language)
    changed = gitdiff.changed_files(base)
    sources = [p for p in changed if preset.is_source(p) and not match_any(p, policy.exempt)]
    tests = [p for p in changed if preset.is_runnable_test(p)]
    lines = gitdiff.changed_lines(base)
    targets = sorted(
        {
            t
            for p in sources
            if p in lines
            for t in preset.mutation_targets(p, lines[p], gitdiff.read_head(p))
        }
    )
    packages = sorted({f"./{PurePosixPath(t).parent}" for t in tests})
    return {
        "tests": " ".join(tests),
        "test-packages": " ".join(packages),
        "mutate": MUTATE_SEPARATOR[language].join(targets),
        "sources-changed": "true" if sources else "false",
    }


def slug(working_directory: str) -> str:
    """A name-safe identifier for the project directory (artifact and cache keys)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", working_directory.strip("./")).strip("-")
    return cleaned or "root"


def build(
    language: str, inputs: dict[str, Any], event: Event, policy: Policy, root: Path
) -> dict[str, str]:
    base = event.base()
    label = str(inputs.get("skip-presence-label") or "")
    return {
        "slug": slug(str(inputs.get("working-directory") or ".")),
        **commands(language, inputs, root),
        **changes(language, base, policy),
        "base": base,
        "event": event.name,
        "repeats": f"{policy.get('flaky.repeats'):g}",
        "presence-report-only": "true" if label and label in event.labels() else "false",
    }


def write_outputs(outputs: dict[str, str], target: Path) -> None:
    with target.open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            if "\n" in value:
                raise ValueError(f"output {key} must be a single line")
            fh.write(f"{key}={value}\n")


def render(language: str, outputs: dict[str, str]) -> str:
    keys = (
        "base",
        "install-command",
        "format-command",
        "lint-command",
        "typecheck-command",
        "test-command",
        "tests",
        "mutate",
    )
    rows = [
        f"| {k} | `{outputs[k].replace('|', '\\|')}` |" if outputs[k] else f"| {k} | _(none)_ |"
        for k in keys
    ]
    return "\n".join([f"### Golden plan ({language})", "", "| | |", "|---|---|", *rows])
