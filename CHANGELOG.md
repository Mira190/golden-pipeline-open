# Changelog

## Unreleased

Scorecard L4 gains three controls (21 in all):

- `merge-queue`: the default branch's rules include a merge queue.
- `code-scanning`: a `pull_request` workflow runs CodeQL or Semgrep, or a `code_scanning`
  ruleset rule names at least one required tool.
- `release-tags`: an active tag ruleset blocks deleting and moving `v*` tags. It is read
  with `Metadata: read`, which every `GITHUB_TOKEN` has.

Proof of alignment:

- `docs/alignment.md` maps 35 published practices (Google, Meta, Uber, Shopify, Amazon, SLSA,
  OpenSSF, DORA) to the control that implements each, with executable evidence, and crosswalks
  the controls to NIST SSDF v1.1, SLSA v1.2, OpenSSF Scorecard, OWASP SAMM v2 and DORA.
  `tests/test_alignment.py` resolves every cited scenario, drill, test, job, control and
  standard identifier, so a renamed scenario or a withdrawn SSDF task fails the suite.

Second-person rules:

- The test-presence opt-out label counts only when someone other than the PR author applied it,
  and only when that can be verified (`golden.optout`). A self-applied or unverifiable label is
  ignored with a warning, and the gate stays enforced.

Signals and hermeticity:

- The Python default test and flake commands use pytest-timeout 2.5.0 with `--timeout=300`, so a
  hung test fails with a stack trace instead of using up the job timeout.
- The plan job reports `change-size`, the lines a reviewer must read. Above 400 lines it adds a
  non-blocking warning that suggests splitting the PR.
- `scripts/drill.py` has a `forged-coverage` scenario: a conftest rewrites `coverage.xml`. Diff
  coverage is fooled by design, and the drill proves that mutation testing still blocks.

Verify before you deploy (SLSA: consumers must be able to validate provenance):

- `release.yml` attaches the Sigstore attestation bundle to the GitHub release, so the wheel's
  provenance can be verified offline (`gh attestation verify --bundle`) and OpenSSF Scorecard's
  Signed-Releases check, which reads release assets, can see it.
- New `actions/verify-image` composite action and `templates/deploy-verify.yml`. A deploy job
  verifies, with `gh attestation verify`, that an image *digest* (tags are refused) carries SLSA
  build provenance and an SBOM attestation signed by this repository's `container.yml` for the
  expected repository, on a GitHub-hosted runner, before it deploys. There is no skip flag.
- `container.yml` smoke-tests and scans the *published* digest before attesting it, so a
  rebuilt image that fails is never attested. Attestations no longer create an artifact storage
  record, which would need an `artifact-metadata: write` grant callers don't give.

Vulnerability gate, diff-aware (the Google OSV model):

- On pull requests and in the merge queue, `security.yml` scans the base commit and the change and
  blocks only on vulnerabilities **the change introduces** (`osv-reporter-action --old/--new`).
  A newly published advisory no longer turns unrelated pull requests red; vulnerabilities already
  in the base are listed in the job summary but don't block. Push, schedule and manual runs still
  block on every known vulnerability, with time-boxed exceptions in `osv-scanner.toml`
  (`ignoreUntil` + `reason`, the same model as waivers). Run the pipeline on a schedule so
  advisories against merged code surface; see docs/configuration.md.
- A scan that fails or writes no readable results fails the gate instead of reading as "no new
  vulnerabilities". Results live under `.git/`, so a pull request cannot supply them.
- A change never grades its own vulnerability exceptions: on pull requests and in the merge
  queue, every `osv-scanner.toml` is read from the base commit, so a PR cannot except a
  vulnerability it introduces. `golden policy check` enforces what OSV-Scanner leaves optional:
  each `[[IgnoredVulns]]` entry needs an `id`, a `reason` and an `ignoreUntil` at most 90 days
  ahead. The policy summary lists the exceptions, and CODEOWNERS covers `**/osv-scanner.toml`.
- A daily `schedule` trigger in the templates, the README snippet and `ci.yml` runs the whole
  pipeline on the default branch, so new advisories and flaky tests surface there first.

Weakening a test is gated:

- Mutation testing now also mutates every source function that a changed or **removed** test
  line names, in the source files that test references. A pull request that only drops
  assertions, deletes a test case or deletes a whole test file used to change no source line,
  so no gate had anything to check. Python (AST), TypeScript/JavaScript and Go (function
  boundaries) are covered. `golden mutation` takes `--preset` to filter Stryker and gremlins
  reports to the same scope; `golden.yml` and `golden local` pass it.

A change never grades itself:

- `golden plan` warns on every workflow, CODEOWNERS or `golden.toml` file the pull request
  changes. GitHub runs the pull request's own copy of a workflow, so a code owner must review
  it; docs/adoption.md now recommends an organisation ruleset that requires the pipeline from a
  governance repository.
- Scorecard L4 gains `fresh-review`: the pull-request rule must dismiss approvals on new pushes
  and require approval of the most recent push, so a second person approves the *final* code
  (two-person review). Ruleset bypass actors stay out of scope: GitHub returns them only to a
  token with write access to the ruleset.

- On pull requests and in the merge queue, every gate reads `golden.toml` **at the base commit**.
  The change may tighten the policy. Anything that loosens it applies only after merge, and is
  listed under "Not applied to this change": a lower threshold, a new or extended waiver, a new
  exemption, or deleting the file. `golden local` plans with the policy at the merge-base too.
- Scorecard L3 gains two controls:
  - `owner-review`: the ruleset requires code-owner review, and CODEOWNERS owns
    `.github/workflows/` and the `golden.toml` in use.
  - `gates-on`: no caller sets `mutation`, `security` or `workflow-lint` to `false`.

  Repositories without code-owner review drop from L3 to L2.

Measured: a new gate decision benchmark (`evals/`, also run by `tests/test_evals.py`). It holds
81 labelled pull requests (legitimate, gaming and malformed), replayed through the real CLI and
scored as escapes, false blocks, robustness failures and crashes. Against v1.0.0 it finds 21
escapes, 16 false blocks and 13 robustness failures, 7 of them tracebacks. All are fixed below.
Two documented gaps remain (see `evals/README.md`).

Fewer false blocks:

- Python test code now includes Django's `tests.py` and helpers under any `tests/` or `test/`
  directory. Relative imports (`from ..core import f`) count as references; comments don't.
- Node: `order.service.ts` is referenced by imports of `./order.service`, and `cart/index.ts` by
  `./cart/index.js`. `src/test/` and nested `tests/` directories are test code.
- Multi-line workflow inputs (a YAML `|` block for `test-command`) are written to job outputs
  with random heredoc delimiters, which also rules out output injection.

More fail-closed checks:

- `golden.toml` rejects fractional `repeats` and levels, values no change could ever meet
  (`min_level = 5`, `diff_min = 101`, a negative ratchet tolerance), and unknown keys inside a
  waiver.
- Scorecard: YAML comments never satisfy a control. A real `cooldown:` or `minimumReleaseAge`
  is required. `permissions: write-all` or a top-level `write` scope fails least privilege.
  `on: [push, pull_request]` counts as PR CI. Lockfiles under `node_modules/`, `vendor/` and
  `.venv/` don't count. Renovate is found in all of its config locations.
- Mutation: `--format mutation-testing-elements` (Stryker.NET, Stryker4s). A malformed report is
  an error. Absolute paths are resolved against the report's `projectRoot`.
- A truncated Cobertura report is an error with a message.
- `golden dora` validates each record and names the bad line. It reports the 2024 metric
  `deployment_rework_rate` from an optional `rework` field.
- Annotation file paths are escaped (`,` and `:`), so the annotations land on the right file.

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
