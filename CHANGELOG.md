# Changelog

## Unreleased

Fail-closed fixes. Each closes a way a gate could pass without checking anything:

- Diff coverage now fails when a changed source file is **missing from the coverage report**.
  Before, diff-cover skipped such files: a new module that no test imported passed as 100%
  covered under the Python and Vitest defaults. The workflow passes `--preset`.
- Git diff parsing: an added line starting with `++ ` no longer redirects later hunks to a fake
  file. File names with spaces, quotes or non-ASCII characters now match between the changed
  files and the changed lines. A user's `diff.noprefix` or `core.quotePath` setting can no longer
  change the parse.
- A coverage report with no coverable lines is an error instead of 100%. So is a malformed
  baseline.
- A mutation run in which no mutant got a verdict (all compile errors), or an interrupted mutmut
  run, fails instead of reporting "no mutants". Report paths `./x` and absolute paths are
  normalised before scoping to changed lines.
- `golden.toml`:
  - Rejected now: unknown keys, non-table sections, non-finite numbers, date-time waiver expiries.
  - Rejected now: blanket `presence.exempt` globs (`**`, `*.py`); a time-boxed waiver replaces
    them.
  - These now exit 2 with an error instead of a traceback.
- The coverage ratchet warns visibly when the baseline is missing (for example, an evicted cache
  entry) instead of skipping silently.
- Scorecard: the controls for provenance and for using the Golden workflow need a real `uses:`
  step; a mention in a comment no longer counts. `pull_request_target` no longer counts as
  running on pull requests.

Shift left:

- New `golden local`: runs the pipeline's effective commands and gates on a developer's machine
  (or in a coding agent's loop) against the merge-base with the default branch. It has
  `--fast`, `--mutation`, `--dry-run` and `--set` modes, and warns when work is uncommitted.
- `.pre-commit-hooks.yaml` with the `golden-policy`, `golden-fast` and `golden-full` hooks.
  `AGENTS.md` and `scripts/local.sh` apply the same loop to this repository.

Pipeline:

- `security.yml` now runs actionlint with shellcheck from a digest-pinned image (input
  `workflow-lint`, default on). This may surface existing problems in callers' workflows.
- Flake gate: only test files the runner collects are re-run. A PR touching only `conftest.py`
  or a fixture no longer fails with "no tests ran".
- `container.yml`: image names are lowercased, so SBOM and attestations no longer fail for
  mixed-case owners.
- `release.yml`: a release requires the tag on the default branch and a passing `ci` check on
  that commit. The wheel and sdist are attached to the release with signed SLSA build provenance.
- CodeQL runs on `merge_group` with a concurrency group. harden-runner was added to the CodeQL
  and Scorecard jobs.
- Templates and README: `pull_request` listens to `labeled`/`unlabeled`, so the test-presence
  opt-out label takes effect without a new push. The container template has a concurrency group.
- This repository's CI:
  - Tests no longer read the real GitHub event (they failed on every PR).
  - The example image is published only after every gate passes.
  - The security scan runs once instead of four times.
  - Vulnerable dev dependencies of the Node example were upgraded.

## 1.0.0 — 2026-10-21

First release.

- `golden.yml`: reusable pipeline for Python (uv), TypeScript/Node and Go with a single
  required `golden` check.
- Gates: diff coverage, test presence, coverage ratchet, flake re-runs, diff-scoped mutation
  testing (mutmut / StrykerJS / gremlins), repository scorecard.
- `security.yml`: gitleaks, OSV-Scanner, dependency review, zizmor.
- `container.yml`: build, smoke test, SBOM, grype, signed SLSA provenance and SBOM attestations.
- Policy as code (`golden.toml`) with platform floors and expiring waivers.
- Gate-effectiveness drills (`scripts/drill.py`) running in CI.
