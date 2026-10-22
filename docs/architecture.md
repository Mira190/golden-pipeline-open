# Architecture

## Job graph

```mermaid
flowchart LR
  subgraph caller["your repository: ci.yml"]
    C["jobs.ci: uses golden.yml@sha"]
  end
  subgraph golden["golden.yml (reusable)"]
    P["plan<br/>policy check, diff base,<br/>effective commands,<br/>changed tests and lines"]
    L["lint<br/>format, lint, types"]
    T["test (matrix)<br/>random order + coverage"]
    G["gates<br/>diff coverage, test presence,<br/>coverage ratchet, flake ×N,<br/>scorecard"]
    M["mutation<br/>changed lines only<br/>(pull requests)"]
    S["security (nested)<br/>gitleaks, OSV,<br/>dependency review, zizmor"]
    V{{"golden<br/>the one required check"}}
    P --> L --> V
    P --> T --> G --> V
    T --> M --> V
    S --> V
    P --> V
  end
  C --> P
```

- `plan` runs first and is cheap (about 20 s). Everything else fans out from it.
- `lint` and `test` run in parallel; `gates` and `mutation` wait for the coverage report from
  `test`.
- `security` is a nested reusable workflow (`$/.github/workflows/security.yml`), so it can also
  be used on its own.
- The final `golden` job `needs` every other job with `if: always()`. It fails when any job
  failed or was cancelled. Skipped jobs are fine: for example, `mutation` is skipped on pushes and
  when no source lines changed. Because a skipped *upstream* job always comes with a failed one,
  nothing slips through.

## One version for YAML and logic

Reusable workflows usually ship shell snippets or download a separately versioned tool, and the
two drift apart. Here the gate logic is a Python package (`src/golden`) inside the same repository.

`actions/setup` is a composite action referenced with the 2026 self-repository syntax,
`uses: $/actions/setup`. It resolves to *this repository at the commit the caller pinned* and
installs the CLI from its own checkout:

```yaml
- name: Install golden CLI
  env:
    GOLDEN_SRC: ${{ github.action_path }}/../..
    UV_PROJECT_ENVIRONMENT: ${{ runner.temp }}/golden-cli
  run: uv sync --project "$GOLDEN_SRC" --locked --no-dev --no-editable --python 3.13
```

When a consumer pins `golden.yml@<sha>`, that one SHA freezes the YAML, the default commands
(defined in `src/golden/plan.py`), the thresholds' floors, the gate algorithms **and every
dependency they run on**. The CLI is installed from that commit's `uv.lock` (hash-checked;
`--locked` fails if the lock is stale), and the build backend is pinned through
`build-constraint-dependencies`. `uv tool install` would re-resolve instead, and pick up a
dependency release newer than the lock and its 7-day cooldown. Only the `golden` entry point is
put on `PATH`, so the CLI's interpreter never shadows the project's. A Dependabot
bump of that SHA upgrades all of them together and shows the whole diff in one PR.

## Where defaults live

Language defaults are data in `src/golden/plan.py`, not conditionals in YAML. `golden plan`
merges them with the caller's inputs (`toJSON(inputs)`) and writes the effective values as job
outputs:

| Input empty? | Effective value |
|---|---|
| yes | language default, e.g. `uvx ruff@0.17.0 check --output-format=github .` |
| no | the caller's command, verbatim |

This makes the defaults reviewable, versioned and unit-tested (`tests/test_plan.py`). It also
means a team can replace any step (for example `test-command: make test`) without forking.

Commands talk to the gates through environment variables, never template expansion inside
`run:` (which zizmor flags as injection-prone):

| Variable | Set for | Meaning |
|---|---|---|
| `GOLDEN_TESTS` | flake command | changed test files (space-separated) |
| `GOLDEN_TEST_PACKAGES` | flake command | packages of changed tests (`./pkg`, for Go) |
| `GOLDEN_REPEATS` | flake command | `flaky.repeats` from policy |
| `GOLDEN_MUTATE` | mutation command | changed functions (mutmut) or `file:start-end` ranges (Stryker) |
| `GOLDEN_BASE` | mutation command | the diff base commit |

Flake and mutation commands run with `bash -o noglob`, so mutmut patterns such as
`pkg.mod.x_func__mutmut_*` reach the tool unexpanded.

## The diff base

| Event | Base | Diff-based gates |
|---|---|---|
| `pull_request` | `pull_request.base.sha` | all |
| `merge_group` | `merge_group.base_sha` | diff coverage, ratchet and flake gate. Test presence and mutation already ran on the PR, and the opt-out label lives on the PR. |
| `push` to the default branch | none | none. The ratchet *saves* a new baseline instead. |

Every diff uses `base...HEAD` (merge-base semantics) with `--relative`, so a project in
`services/api/` sees only its own files. Annotations are rewritten back to repository-relative
paths so they land on the right line in the PR.

## Data flow between jobs

| Data | Producer | Consumer | Mechanism |
|---|---|---|---|
| Coverage report | `test` (newest version in the matrix) | `gates` | artifact `golden-coverage-<slug>`, kept 7 days |
| Default-branch coverage baseline | `gates` on push to the default branch | `gates` on PRs | Actions cache key `golden-baseline-<slug>-<sha>`, restored by prefix |
| Effective commands, changed files | `plan` | all jobs | job outputs |

The baseline uses the cache rather than a committed file. A committed baseline needs a bot with
write access and causes merge conflicts. PRs can read caches written on their base branch, but
caches written by PRs are scoped to the PR's own ref and never reach the default branch. If the
cache was evicted (7 days unused), the ratchet reports "no baseline yet" and passes; the next
push to the default branch restores it.

## Language specifics

| | Python | Node | Go |
|---|---|---|---|
| Toolchain | uv (pinned 0.13.1), Python from the matrix | Node from the matrix, package manager from the lockfile | `go.mod` |
| Install | `uv sync --locked` | `npm ci --ignore-scripts` (pnpm/yarn/bun equivalents) | `go mod download` |
| Coverage format | Cobertura (coverage.py) | Cobertura (v8 provider) | coverprofile → Cobertura (gocover-cobertura) |
| Mutation scope | changed **functions** → mutmut names | changed **line ranges** → `stryker --mutate file:a-b` | whole module, then `golden mutation --base` keeps changed lines |
| Mutation report | `mutants/mutmut-cicd-stats.json` | `reports/mutation/mutation.json` | `gremlins.json` |

Why Go mutates the whole module: gremlins' own `--diff` matches no files when the module lives in a
subdirectory of the repository. We verified this in a drill. Filtering the full report by changed
lines works in any layout, at the cost of mutation time on large modules.

## Post-submit: flaky-test scan

The flake gate re-runs only the tests a pull request *changed*. Nothing in the merge path measures
the flakiness of the suite that already exists, so a flaky old test fails unrelated pull requests
and nobody knows which one it is. Meta measures flakiness continuously from repeated runs and
routes it to owners ("Probabilistic Flakiness", 2020), Azure DevOps marks tests that fail and pass
on the same code, and Google tracks its flaky rate (about 0.15%, *SWE at Google* ch. 11) and runs
post-submit checks on the main line (ch. 23). `scheduled.yml` is that loop. A consumer calls it
nightly from its default branch (`templates/ci-scheduled.yml`), and it is not a required check.

```mermaid
flowchart LR
  T["schedule or manual run<br/>default branch"] --> P["plan<br/>toolchain, install command"]
  P --> R["scan: run the suite N times<br/>random order, one report per run"]
  R --> S["golden flaky-scan<br/>pass and fail on one commit?"]
  S --> O["job summary, annotations,<br/>artifact: reports + JSON"]
  S --> V{{"exit 0 stable<br/>exit 1 red<br/>exit 2 no verdict"}}
```

| Input | Default | Meaning |
|---|---|---|
| `language` | required | `python`, `node` or `go` |
| `working-directory`, `versions`, `install-command` | as in `golden.yml` | the last version in `versions` runs the scan |
| `runs` | 5 | how many times to run the suite (at least 2; one run cannot tell flaky from failing) |
| `test-command` | language default | runs the suite once and writes the report to `$GOLDEN_REPORT` |
| `timeout-minutes` | 120 | limit for all runs together |

- **The loop.** A bash loop runs the command `runs` times, each in its own process with its own
  shuffle seed, each writing `run-<i>.xml` (`.json` for Go) to a temporary directory. A red run
  does not stop the loop, because a test that fails once and passes next time is the thing being
  looked for. Every exit status is logged. A run that crashed before writing its report is not
  hidden: the scan fails on a missing report.
- **Default commands** live in `REPORT_COMMANDS` in `src/golden/flakescan.py` and are read with
  `golden flaky-scan --print-command LANGUAGE`: pytest with `-p randomly --junitxml`, Vitest with
  `--sequence.shuffle --reporter=junit`, and `go test -shuffle=on -count=1 -json ./...`. They are
  the *whole-suite* counterparts of the merge gates' commands, so no coverage and no
  parallelism: a flaky test is found by repetition, not by load.
- **Formats** are detected from the content of each report: JUnit XML (pytest, Vitest) and
  `go test -json`. A test is `classname::name` for JUnit and `package/TestName` for Go. A
  `<failure>` or `<error>` is a failure and `<skipped>` a skip. For Go, only `pass`, `fail` and
  `skip` events count; a test that failed only because its subtest failed is not listed twice, and
  a package that fails without any failing test (a build error) is a failure named
  `package/(package)`. A test that appears twice in one report is judged by its worst result.
- **Verdict.** Across the runs, a test is **flaky** when it passed at least once and failed at
  least once, and **always failing** when it failed and never passed. Skips count for neither.
  The flaky rate is flaky tests divided by tests seen, the number to compare with the flake-rate
  target in [evaluation](evaluation.md). Both findings exit 1: an always-failing test is a red
  default branch, which this job should not report as healthy.
- **Fails closed.** No report file, a missing or unparsable report, a report with no tests in it
  and fewer than two runs all exit 2 with the reason. None of them reads as "no flaky tests".
- **Output.** A markdown table in the job summary (at most 50 rows per kind), one `::warning::`
  annotation per flaky test and one `::error::` per always-failing test (also capped at 50), and
  `flaky-scan.json` with every finding, uploaded with the reports as the artifact
  `golden-flaky-scan-<slug>` for 7 days.
- **No write access.** The workflow has `contents: read` and nothing else, so it opens no issues
  and posts no comments. The red scheduled run and its summary are the signal. Route them to
  owners with whatever your team already watches: failed-workflow notifications, or a
  scheduled-run alert in chat.

What it is not: it does not quarantine tests, retry them or block merges, and it never turns a
failing run green. Repeating the suite on one runner also cannot find flakiness that needs other
hardware or heavy parallel load. To reproduce the load your CI applies, set `test-command` (for
example pytest with `-n auto`).

## Decisions and their alternatives

| Decision | Alternative considered | Why |
|---|---|---|
| One `golden.yml` with `language:` | one workflow per language | One required check name, one place to fix bugs. Language differences are data in `plan.py`. |
| CLI in Python | bash in YAML | Testable (230 tests, mutation-tested), typed, cross-language |
| Fail closed | warn on tool errors | A crashed mutation tool or a missing report must never read as "no mutants" |
| Floor + expiring waivers | free-form thresholds | Teams can tighten freely; loosening is visible, justified and temporary |
| Stryker's score formula everywhere | each tool's own (gremlins "efficacy" excludes timeouts) | Same number means the same thing in every language |
| Timeouts count as detected, unless most mutants time out | always detected | A miscalibrated timeout otherwise passes a run that tested nothing; seen with gremlins |
| `ubuntu-24.04` pinned | `ubuntu-latest` | `ubuntu-latest` moved to 26.04 during October–November 2026; image changes should be deliberate |
