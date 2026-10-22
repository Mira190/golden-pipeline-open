# Gates

Every gate prints a markdown verdict to the job summary and annotates the offending lines in the
pull request. It exits non-zero only when the merge must be blocked. A gate with an active
[waiver](configuration.md#waivers) reports `failed (waived)` and lets the merge through.

## Diff coverage

**Question:** is the new code executed by tests?

- Runs diff-cover against the coverage report from the newest version in the test matrix,
  comparing `base...HEAD`.
- Passes when the changed coverable lines are covered at `coverage.diff_min` or above (default
  90%, floor 80%), or when the change has no coverable lines (docs, config, comments).
- Each uncovered changed line gets a warning annotation (up to 50 per file).
- **Fails closed on unmeasured files.** diff-cover skips files the coverage report doesn't
  mention, so a new module that no test imports would count as "100% covered". This is the
  default with pytest-cov when `[tool.coverage.run] source` isn't set, and with Vitest when
  `coverage.include` isn't set. The gate therefore also fails when a changed source file (per
  the language preset, minus `presence.exempt`) is missing from the report, and annotates the
  file.

**Fixing a failure:** write a test that exercises the annotated lines. If a line is genuinely
untestable (defensive `raise`, platform branch), exclude it the way your coverage tool supports
(`# pragma: no cover`, `/* v8 ignore next */`). Reviewers see the exclusion in the diff.
For a file that is *missing from the report*, either test it or configure coverage to measure
every source file (`source = ["your_package"]`, `coverage.include: ["src/**"]`). If it has no
runtime code (type declarations, generated code), add a `presence.exempt` glob.

## Test presence

**Question:** did someone write a test for this change?

Every changed source file needs at least one *changed* test file in the same PR that references it:

| Language | A test references `src/x.ext` when… |
|---|---|
| Python | the test imports the module (absolute or relative: `from .x import`, `from .. import x`), or its code mentions the dotted module (`pkg.x`, also inside strings such as `mock.patch("pkg.x.f")`) or the module name as a word. Comments don't count. |
| Node | the test is named `x.test.*` / `x.spec.*`, or imports a path ending in `/x` (with or without extension; `order.service.ts` needs `/order.service`; `cart/index.ts` accepts `/cart` and `/cart/index`) |
| Go | the test file lives in the same package directory |

Ignored automatically: generated code (`*.pb.go`, `*_gen.go`, `*.d.ts`), config files, `vendor/`,
`node_modules/`, migrations, tool work directories (`mutants/`, `.stryker-tmp/`). Add more with
`presence.exempt` globs.

**Escape hatch:** the PR label `no-tests-needed` (configurable with `skip-presence-label`) makes the
gate report-only for that PR. This is meant for pure refactors covered by existing tests. The label
and the report-only verdict are visible to reviewers, and diff coverage still applies.

**The label needs a second person.** It counts only when someone other than the PR author applied
it. On the `labeled` event the applier comes from the event itself. On later pushes it comes from
the issue events API, which the `contents: read` token can read on public repositories. When the
applier can't be verified (for example, on a private repository whose token can't read issue
events), the label is ignored and the gate is enforced. The plan summary and an annotation say who
applied the label, or why it was ignored. A time-boxed `[waivers.test-presence]` in `golden.toml`
is the alternative that always works, because it merges only with a code owner's review.

Adding or
removing the label re-runs the pipeline only when the caller's `pull_request` trigger lists the
`labeled` and `unlabeled` activity types, as the [templates](../templates) do.

## Coverage ratchet

**Question:** did total coverage go down?

- On every push to the default branch, total line coverage is saved as the baseline (Actions cache).
- On pull requests, total coverage may not fall more than `coverage.ratchet_tolerance` (default
  0.5 pp, floor 2 pp) below the baseline.
- With no baseline yet (new repository, or cache evicted), the gate reports and passes.

This catches what diff coverage can't see: deleting tests, or a change that stops exercising
untouched code (Hilton et al., ASE 2018).

## Flake gate

**Question:** are the new tests trustworthy?

Every changed test file is re-run `flaky.repeats` times (default 10, floor 3) in random order. A
single failure blocks the merge. Only files a test runner can execute on their own are re-run:
`test_*.py`, `*_test.py`, `tests.py`; `*.test.*`, `*.spec.*`; `*_test.go`.
A change to a `conftest.py`, a helper or a fixture alone skips the step, because pytest and
Vitest fail when given a file with no tests in it. Most flaky tests are flaky from the moment
they're written (Luo et al., FSE 2014), so this is the cheapest point to catch them.

| Language | Default re-run |
|---|---|
| Python | `pytest -n auto -p randomly --count=N <files>` (pytest-repeat) |
| Node | N fresh `vitest run --sequence.shuffle <files>` processes |
| Go | `go test -race -shuffle=on -count=N <packages>` |

### After the merge: the flaky-test scan

The flake gate covers the tests a change touches. Tests that turned flaky later, through a
dependency, a timing change or a shared fixture, are found after the merge.

- **The run.** [`scheduled.yml`](../.github/workflows/scheduled.yml), called nightly from
  [`templates/ci-scheduled.yml`](../templates/ci-scheduled.yml), runs the whole suite `runs`
  times (default 5) in random order on one commit of the default branch.
- **The comparison.** `golden flaky-scan` compares the JUnit XML or `go test -json` reports:
  - A test that both passed and failed is **flaky**.
  - A test that failed in every run is **always failing**, which means the default branch is red.
- **The outcome.** Either finding turns the run red and lists the tests and the flaky rate in the
  job summary.
- **Fail closed.** Fewer than two reports, or a missing, empty or unparsable report, exits 2 and
  never reads as clean.

The scan is not a merge gate and needs only `contents: read`. Track the rate: Google reports
that "as you approach 1% flakiness, the tests begin to lose value", and that its own rate
"hovers around 0.15%" ([SWE at Google, ch. 11](https://abseil.io/resources/swe-book/html/ch11.html)).
Re-running a failed test only "delay[s] the need to address the root cause", so the scan reports
flaky tests rather than retrying them away.

## Mutation testing

**Question:** would these tests notice a bug?

The mutation tool injects small faults (`>` → `>=`, `+` → `-`, `return x` → `return None`)
**into the code the change puts at stake** and runs the tests against each one:

- the changed source lines, and
- every source function that a changed or removed *test* line names, in a source file that test
  references. A pull request that only weakens a test (drops an assertion, deletes a test case
  or a whole test file) changes no source line, but it changes what the tests prove about those
  functions, so they are mutated too. Diff coverage and test presence have nothing to check in
  such a PR, and the coverage ratchet doesn't move, so mutation testing is the gate that sees it.

Function boundaries come from the Python AST, from gofmt's layout for Go, and from a
formatter-shaped scan of TypeScript/JavaScript (declarations, function-valued `const`s and class
methods). The gate passes when the score reaches `mutation.min_score` (default 80%, floor 60%):

```
score = (killed + timed out) / (killed + timed out + survived + not covered)
```

Compile errors, runtime errors, ignored and skipped mutants are excluded. The definition is
Stryker's, applied to all three tools so the number means the same thing everywhere.

| Language | Tool | How it is scoped |
|---|---|---|
| Python | mutmut 3.8 | changed functions and methods → `mutmut run "pkg.mod.x_func__mutmut_*"`; survivors are read from `mutants/` |
| Node | StrykerJS 10 | changed line ranges → `stryker run --mutate "src/a.ts:10-25,…"` |
| Go | gremlins 0.6 | whole module, then `golden mutation --base` keeps mutants on changed lines |

Any report in the [mutation-testing-elements](https://github.com/stryker-mutator/mutation-testing-elements)
schema works with `--format mutation-testing-elements` (an alias of `stryker`): StrykerJS,
Stryker.NET and Stryker4s emit it. Absolute file paths in the report are made relative to its
`projectRoot` or the working directory before filtering by the diff.

**Fails closed:**

- A missing report is an error ("did the mutation tool run?").
- A malformed report (a mutant without a location, a wrong type) is an error, never "no mutants".
- A run where more than half of the mutants time out is marked unreliable and fails. The fix is
  to raise the tool's timeout.

**Fixing a failure:** each surviving mutant is annotated on its line with the mutator name. Write
the assertion that would have failed. For example, a surviving `CONDITIONALS_BOUNDARY` on
`if qty >= 50` means no test checks quantity 49 *and* 50.

**Survivors on the line.** Every surviving mutant in scope is annotated on its source line and
listed in the verdict, at most 50, whether or not the score passes. The threshold decides the
merge; the annotations are for the reviewer. Google's mutation testing works the same way: it
shows survivors as code-review findings. In the
[real-fault benchmark](../evals/realfaults/README.md), a mutant survived on the bug's own line
in 6 of 10 real bugs, and in two of those the score was above 80%.

- **Stryker, mutation-testing-elements and gremlins reports** carry the line.
- **mutmut's stats file** has totals only. For Python, `golden mutation` also reads what mutmut
  leaves next to it in `mutants/`, and never runs mutmut again:
  - The exit code of each mutant, from `<path>.meta`. Survivors are 0; 5 and 33 mean no test
    reaches the function (`mutmut.stats.status_by_exit_code`).
  - The mutated module, `mutants/<path>`. The gate finds the first line where a mutant differs
    from the function's `x_f__mutmut_orig` copy and adds that offset to the function's `def` line
    in the source.
  - mutmut has no mutator names, so the label is the mutant key. `mutmut show KEY` prints the
    change.
  - The score, and so the verdict, still comes from the stats file.
- **When a Python survivor can't be placed:**
  - A line is used only if the `__mutmut_orig` copy matches the working-tree function character
    for character. If not, the survivor is listed by name and never annotated on a guess.
  - If the `mutants/` tree is missing, or a `.meta` file is unreadable or malformed, the verdict
    says the survivors could not be located. This is never an error.

**Arid code.** On some lines no reasonable test should care, such as log messages, debug output
and help text. Their mutants are noise, not missing assertions. Google found that filtering these
*arid nodes* is what turns mutation testing from noise (15% of mutants useful) into signal (89%).

- **Built in, for Stryker, mutation-testing-elements and gremlins reports.**
  - `golden mutation` reads the source line of every in-scope mutant. It drops the mutant before
    scoring when the whole line is one logging or debug call.
  - JS/TS calls: `console.log/debug/info/warn/error/trace(…)`, `debug(…)`, and
    `logger.<level>(…)` or `log.<level>(…)`.
  - Go calls: `log.Print*/Fatal*/Panic*`, `fmt.Print*`, `slog.<level>(…)` and
    `logger.<level>(…)`. `fmt.Sprintf`, `Errorf` and `Fprintf` stay mutated, because they build
    values tests may check.
  - Dropped mutants count as neither killed nor survived, and the gate reports how many.
- **The filter is conservative.** A line is arid only when the call's arguments merely name
  values. These lines all stay mutated:
  - a nested call, such as `console.log(applyDiscount(x))`
  - a template interpolation (`${…}`)
  - an assignment, `++`, `await` or `<-`
  - `return x || console.log(y)` and `if (a) console.log(b)`
  - multi-line calls, and lines with a trailing comment

  The benchmark checks both directions:
  - `mutation/arid-log-line-is-not-a-missing-test` must pass.
  - `mutation/logic-inside-a-log-call-stays-mutated` must block.
- **Unreadable source.** If a source file can't be read, nothing is excluded.
- **Opting out.** `--no-arid-filter` turns the filter off.
- **mutmut (Python).** The built-in filter reads JS/TS and Go lines, so it doesn't apply to
  mutmut. Exclude arid lines when the mutants are generated, with `# pragma: no mutate` or
  `do_not_mutate_patterns`, anchored as in this repository's `pyproject.toml`.
- **Anything else:** `// Stryker disable next-line`, or `--exclude-files` for gremlins. Keep the
  list short and commented.

## Static analysis on the diff (optional)

**Question:** does this change introduce a static-analysis finding?

`golden sast` reads SARIF 2.1.0 reports and keeps only the findings on lines the change added or
modified. The reports can come from CodeQL through
[`sast.yml`](../.github/workflows/sast.yml), or from Semgrep, Ruff, golangci-lint or ESLint.

- **Blocks:** a finding on a changed line at or above `min-level` (default `error`).
- **Doesn't block:** findings in untouched code. They are counted as "N pre-existing findings
  outside this change".
- **Skipped:** results suppressed in the source (an absent or `accepted` suppression status), and
  results a baseline already knows.
- **Fails closed:** a missing, empty or non-SARIF report, a result without a location or an
  unknown level is an error.
- **Waiver:** `[waivers.sast]`.

**Why only the diff.** A finding shown on the line the author just edited, at review time, gets
fixed. A backlog of old findings doesn't.
[Tricorder](https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/) and
[Infer](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/) both report this way.
Setup is in [configuration](configuration.md#static-analysis-on-the-diff-optional).

## Repository scorecard

**Question:** is the repository set up so that the gates can't be bypassed?

`golden scorecard` checks 21 controls in four cumulative levels and fails below
`scorecard.min_level` (default 2):

| Level | Controls |
|---|---|
| **L1 Adopted** | CI on `pull_request` (not `pull_request_target`) · calls `golden.yml` · dependencies locked (lockfiles under `node_modules/`, `vendor/`, `.venv/` don't count) |
| **L2 Owned** | CODEOWNERS · Dependabot/Renovate (any of its config locations) · SECURITY.md |
| **L3 Enforced** | ruleset requires the `golden` check · ruleset requires an approval · **ruleset requires code-owner review, and CODEOWNERS owns `.github/workflows/` and the `golden.toml` in use** · force-push and deletion blocked · every action pinned to a SHA · every workflow's top-level `permissions:` is read-only (`write-all` or a top-level `write` scope fails; grant writes per job) · **no gate switched off in the caller** (`mutation: false` in any caller; `security` or `workflow-lint: false` in *every* caller, since one repository-wide scan is enough) · dependency cooldown configured |
| **L4 Verified** | release artifacts carry signed provenance (an `actions/attest*` step or a `container.yml` call) · OpenSSF Scorecard runs (an `ossf/scorecard-action` step) · no active waivers · **the pull-request rule dismisses approvals on new pushes (`dismiss_stale_reviews_on_push`) and bars the last pusher from approving their own push (`require_last_push_approval`)** · **a merge queue** (the default branch's rules include `merge_queue`) · **static analysis on pull requests** (a `pull_request` workflow runs `github/codeql-action/analyze`, a Semgrep action or a `semgrep` command, or the rules include a `code_scanning` rule naming at least one tool) · **release tags can't be moved or deleted** (one active tag ruleset with `deletion` plus `non_fast_forward` or `update` on `~ALL` or `refs/tags/v*`, with no exclusion that hits `v1.0.0`) |

Workflow files are read with YAML comments removed, and step-based controls look at `uses:`
references, so a commented-out line or a mention in a `run:` script never satisfies a control.

The ruleset controls are read from the GitHub API: `GET /repos/{repo}/rules/branches/{branch}`
for the branch rules, and `GET /repos/{repo}/rulesets?targets=tag&includes_parents=true` plus one
`GET /repos/{repo}/rulesets/{id}` per tag ruleset (the list returns only summaries). Both need
only `Metadata: read`, which every `contents: read` token has. When the API isn't reachable they
show **unknown**, and unknown never counts as a pass. Static analysis is the exception: a
workflow that runs it on pull requests counts without the API.

**Why code-owner review is an L3 control.** On a `pull_request` event GitHub runs the
workflow file *from the pull request*. A PR can therefore edit its own `ci.yml` to switch a gate
off or replace `test-command`, and the `golden` check still goes green. Only a ruleset that
requires a code owner's approval for `.github/workflows/` (or an organisation ruleset that
requires the workflow from a central repository) closes that path. `golden.toml` is different:
the gates read it from the pull request's base commit, so a PR cannot loosen it for itself
(see [configuration](configuration.md#a-change-never-grades-itself)).

**Why fresh review is an L4 control.** Two-person review, a second person approving the *final*
code, is the core of separation of duties, and a required approval alone doesn't deliver it:
the author can collect an approval on a harmless first commit and then push the real change
(`dismiss_stale_reviews_on_push` drops the approval), or push last and approve their own push
(`require_last_push_approval` blocks that). Both parameters are read from the same
`pull_request` rule as the approval count. The scorecard can't see **ruleset bypass actors**:
GitHub returns `bypass_actors` only to a caller with write access to the ruleset, not to the
`contents: read` token CI runs with, so review that list in the ruleset settings yourself.

**Why a merge queue, static analysis and immutable tags are L4 controls.**

- **Merge queue.** Required checks test a pull request against its base *as it was*. When
  several pull requests merge close together, each can be green and the combination red. A merge
  queue re-tests every pull request on top of the ones ahead of it. This is how Uber
  ([SubmitQueue](https://www.masoud.io/docs/eurosys19.pdf)) and Shopify keep a busy trunk green.
  GitHub offers merge queues only to organisation-owned public repositories and to private
  repositories on GitHub Enterprise Cloud, so a personal repository stops at L3.
- **Static analysis on the pull request.** Findings shown at review time get fixed; findings in
  an offline list don't ([Tricorder](https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/),
  [Infer](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/)). CodeQL's default
  setup has no workflow file, so use advanced setup or a `code_scanning` ruleset rule.
- **Immutable release tags.** Consumers pin to tags, and provenance attests what a tag built. If
  a tag can be moved, an attested artifact no longer matches its version. SLSA's source track
  requires that tags "be prevented from being moved or deleted".

As with the pull-request rule, bypass actors are invisible to a `contents: read` token, so review
them in the ruleset settings.

## Security (nested workflow)

| Check | Tool | Blocks on |
|---|---|---|
| Committed secrets | gitleaks 8.30.1 (digest-pinned image); PR range on PRs, full history on pushes | any finding |
| Vulnerable dependencies | OSV-Scanner 2.7 over every lockfile in the project directory | pull requests and the merge queue: a vulnerability the change introduces. Push, schedule and manual runs: any known vulnerability not excepted in `osv-scanner.toml` |
| New dependencies in the PR | dependency-review-action | severity ≥ `high` (configurable); licences outside `allow-licenses` if set |
| Workflow security | zizmor 1.30 (`regular` persona) | any finding (template injection, excessive permissions, unpinned actions, cache poisoning, …) |
| Workflow correctness | actionlint 1.7.12 with shellcheck (digest-pinned image) | any finding (invalid expressions, unknown inputs, shell bugs in `run:`); input `workflow-lint` |
| Go reachability | govulncheck 1.9 (in the Go lint step) | a reachable vulnerable symbol |

### Vulnerable dependencies: what the change adds, then everything

A new advisory can land at any time. If every pull request failed on every known vulnerability,
each advisory would turn unrelated PRs red with no code change, and people learn to bypass a gate
that cries wolf. So the gate follows the practice of Google's OSV-Scanner action:

- **Pull requests and the merge queue** scan the base commit and the change, then diff the two.
  Only a vulnerability present in the change and absent from the base blocks the merge. Each one
  gets an error annotation on its lockfile.
- **Pre-existing vulnerabilities are shown, not hidden.** The job summary lists them under
  "Already present in the base (not blocking)", and one warning annotation gives the count.
- **Push, schedule and manual runs** scan the checked-out commit and block on every known
  vulnerability. This is where an advisory against code you already merged is caught, so
  [run it on a schedule](configuration.md#securityyml) too.
- **Exceptions are time-boxed** and live in `osv-scanner.toml` next to the lockfile, with a
  `reason` and an `ignoreUntil` date: the same reason-plus-expiry model as a
  [waiver](configuration.md#waivers), where `expires` is at most 90 days ahead. See
  [`osv-scanner.toml`](configuration.md#osv-scannertoml) for the file and its limits.

**Fails closed:** a scan that writes no results (OSV API unreachable), or a scan of the change
that fails without reporting anything (an unparsable lockfile), is an error, never "no new
vulnerabilities". A project that has no lockfiles in the base, because the change creates it, is
not an error: everything in the change counts as new.

**Fixing a failure:** upgrade the annotated package to the fixed version in the table. If the
vulnerable code is genuinely not reachable, add an exception with a reason and a date, and expect
the reviewer to question it.

A scan step shown as failed with a green job is normal: osv-scanner exits 1 whenever it finds
anything, including vulnerabilities that were already there. The "New vulnerabilities" step is the
verdict.

## Change size (signal, not a gate)

**Question:** can a reviewer still read this change properly?

The plan job counts the changed lines a reviewer has to read. Lockfiles, snapshots and
`presence.exempt` files are left out. Above 400 lines it adds a warning annotation that suggests
splitting the PR. It never blocks: at large engineering organisations small changes are a norm
backed by tooling, not a hard limit. A mechanical rename or a generated migration is legitimately
large. Review effectiveness does fall off sharply past a few hundred lines (Google's small-CL
guidance; the SmartBear/Cisco study of 2,500 reviews), and AI-assisted authoring makes large diffs
cheap to produce, which makes the signal more valuable.

## Verdict

The `golden` job aggregates everything above. Make it the **only** required status check:

- Adding or removing gates never requires editing rulesets.
- A cancelled or failed job can never be silently ignored.
