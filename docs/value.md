# Does it deliver value?

A quality gate is worth its cost only if it does three things:

- **Catches what it claims to catch.**
- **Leaves legitimate work alone.**
- **Stays cheap enough to run on every change.**

This page measures all three. Every number comes from a command in this repository that you can
re-run, and the limits of each measurement are stated with it.

| Claim | Measurement | Result | Re-run |
|---|---|---|---|
| It makes the right merge decision | [Decision benchmark](#1-the-right-merge-decision): 96 labelled pull requests through the real CLI | 0 escapes, 0 false blocks, 0 crashes; v1.0.0 had 31, 20 and 7 | `uv run python evals/runner.py` |
| It catches weak tests that coverage misses | [Drills](#2-weak-tests-with-real-toolchains): 4 changes × 3 languages, with pytest, Vitest, Go and the mutation tools | Coverage passes the weak test in all three languages; mutation blocks it. 10/10 drills reach the expected decision | `uv run python scripts/drill.py <language>` |
| It catches real bugs | [Real-fault benchmark](#3-real-bugs-from-real-projects): 10 real bugs from open-source libraries | The full pipeline flags 4, coverage alone 3; a surviving mutant sits on the faulty line in 6 | `uv run python evals/realfaults/run.py` |
| It doesn't block legitimate work | 38 legitimate pull requests in the benchmark | 0 false blocks; v1.0.0 got 20 wrong | as above |
| It holds against people who want to get round it | 42 gaming attempts in the benchmark | 0 escapes; v1.0.0 let 31 through | as above |
| It fails closed | 16 corrupt or malformed inputs | 0 crashes, 0 silent passes | as above |

## 1. The right merge decision

The [decision benchmark](../evals/README.md) holds 96 labelled pull requests:

- **38 legitimate changes** the gates must let through or handle correctly.
- **42 attempts to get round a gate.** For example: no tests, assertion-free tests, a pull request
  that lowers its own threshold or waives itself, a scorecard control met by a comment.
- **16 broken inputs** the gates must refuse with a message, not crash on or wave through.

Each scenario builds a real git repository and runs the `golden` CLI as `golden.yml` does. The
same scenarios run against the initial release and against the current code:

| | Correct | Escapes | False blocks | Robustness failures | Crashes | Known gaps |
|---|---|---|---|---|---|---|
| v1.0.0 (initial release) | 31 / 96 | **31** | **20** | 13 | 7 | 1 |
| Current | 95 / 96 | 0 | 0 | 0 | 0 | 1 |

v1.0.0's 64 wrong decisions fall into two groups, and the split matters for what the number
proves:

| | Escapes | False blocks | Robustness (incl. crashes) | Total |
|---|---|---|---|---|
| **Defects in controls v1.0.0 already had** | 20 | 13 | 13 | **46** |
| Controls v1.0.0 did not have | 11 | 7 | 0 | 18 |

The 46 are the measure of the method's value. They are bugs in gates that had unit tests,
99% branch coverage and an 80% mutation score. Examples:

- A pull request could lower its own threshold, add its own waiver or exemption, or delete
  `golden.toml`, and pass every gate.
- A test that mentioned a module in a comment satisfied test presence.
- A scorecard control was met by `# TODO: container.yml`.

Unit tests ask whether the code does what its author meant. Only scenario-level ground truth
asks whether the merge decision is right. The 18 are new controls, each written test-first
against a scenario that failed on the old code:

- Test-only weakening.
- Self-gating warnings.
- The opt-out label needing a second person.
- Owned OSV exceptions.
- Caller-disabled gates.
- The arid-line filter.
- Python surviving mutants shown on their line.

The one known gap is reported and tested as a strict expected failure.
`presence/python-string-mention`: a module name in an unrelated string still counts as a
reference, because `mock.patch("pkg.mod.f")` is how Python names a patch target. Mutation testing
is the backstop.

## 2. Weak tests with real toolchains

`scripts/drill.py` builds a small project in each language and pushes four kinds of change
through the gates with the real toolchains:

- **Python:** pytest, coverage.py and mutmut.
- **TypeScript:** Vitest, V8 coverage and StrykerJS.
- **Go:** `go test` and gremlins.

The function under test is a tiered discount, `bulk_discount_percent`: 10+ units get 5% off,
50+ units get 10%. The four changes:

- **no-tests:** the function ships without a test.
- **weak:** a test runs the function at 1, 10 and 50 units. It asserts only that a result
  exists (`is not None`, `toBeDefined()`), or in Go nothing at all. Every line is covered and
  nothing is checked.
- **strong:** a test asserts each tier and both boundaries.
- **forged-coverage:** a `conftest.py` rewrites `coverage.xml` so that every line reads as
  covered.

| Language | Change | Diff coverage | Test presence | Mutation | Merge |
|---|---|---|---|---|---|
| Python | no-tests | **block** | **block** | **block** | blocked |
| Python | weak | pass | pass | **block** | blocked |
| Python | strong | pass | pass | pass | allowed |
| Python | forged-coverage | pass | pass | **block** | blocked |
| TypeScript | no-tests | **block** | **block** | **block** | blocked |
| TypeScript | weak | pass | pass | **block** | blocked |
| TypeScript | strong | pass | pass | pass | allowed |
| Go | no-tests | **block** | **block** | **block** | blocked |
| Go | weak | pass | pass | **block** | blocked |
| Go | strong | pass | pass | pass | allowed |

10 of 10 drills reached the expected decision on 2026-10-22. They took 2 min 30 s for Python
(including the forged report), 64 s for TypeScript and 58 s for Go, end to end, including tool
start-up.

**The drills show why one gate isn't enough.**

- **Coverage alone:** a coverage-only pipeline, the most common "quality gate" in practice,
  merges the weak test in all three languages.
- **Forged report:** diff coverage and test presence can't detect a forged coverage report
  either, because the code under test writes the report.
- **Mutation testing:** it re-runs the tests under its own control and blocks both.
- **Mutation alone:** it costs more and needs tests to exist. Test presence and diff coverage
  answer in seconds and point at the exact lines.

The gates are layered so that each covers the others' blind spots.

## 3. Real bugs from real projects

The [real-fault benchmark](../evals/realfaults/README.md) asks the question the decision benchmark
can't: for bugs nobody planted, would the gates have flagged the weak tests at the code where the
bug lived?

- **Source:** 10 real bug fixes from four Python libraries: boltons (4), toolz (2), tabulate (2)
  and semver (2). They were screened by fixed rules from 77 candidate commits across five
  projects.
- **Method:**
  - Each bug runs at the fix's parent, with the bug live and the tests of that day.
  - The gates are the platform defaults: diff coverage ≥ 90% and mutation ≥ 80% on the changed
    functions.
  - Every regression test was verified to fail before the fix and pass after it.

| Over 10 real bugs | Result |
|---|---|
| Faulty lines already executed by the tests of the day | 7 / 10 |
| A coverage-only pipeline flags the faulty code | **3 / 10** |
| The full pipeline (diff coverage + mutation) flags it | **4 / 10** |
| A mutant survives on the bug's own line | **6 / 10** |
| The fix itself is a surviving mutant (`boltons-bits-length`, `>` to `>=`) | 1 / 10 |
| Missed by both gates | 6 / 10 |

**What this shows:**

- **Coverage says little about real bugs.** In 7 of 10 the faulty lines were already covered.
  Coverage flagged only the three bugs in code no test ran, the easy case.
- **Mutation testing added one catch to coverage.** In `boltons-bits-length` the suite ran the
  comparison but never pinned its boundary. The fix, `>` to `>=`, is one of the mutants no test
  killed.
- **The mutants pointed at the bug more often than the gate blocked.** A mutant survived on the
  faulty line in 6 of 10 bugs. In two of those the function scored above 80%, so the gate passed.
  - This is how Google uses mutation testing: surviving mutants go to the reviewer as comments on
    the line, more than as a pass/fail score.
  - The benchmark also showed that `golden mutation` did not surface Python survivors at all. It
    read only mutmut's totals.
  - The gate now shows them on the line, as it already did for TypeScript and Go. It reads
    mutmut's per-mutant results and never guesses a line. The benchmark scenario
    `mutation/mutmut-survivor-annotated-on-passing-score` checks it.
- **Four bugs are out of reach for both gates.** Two needed code that wasn't there, a missing
  `try`/`except` and a missing guard against a hang, and no mutation operator writes missing
  code. In the two semver bugs every mutant on the faulty line was killed, yet an ordering
  property was still violated. Those bugs need property-based or example tests that a reviewer
  asks for.

**Limits:**

- The sample is small, and it covers recent edge-case fixes in five small libraries.
- The bug-introducing change isn't reconstructed, so test presence isn't evaluated.
- Each bug was run once.
- Four screened bugs weren't run within the time budget; the manifest says why for each.

Treat these numbers as an existence proof with an honest denominator, not as a rate.

## 4. What it costs

Measured on a 4-core cloud container on 2026-10-22. Timings on GitHub-hosted runners will
differ.

| What | Time | Note |
|---|---|---|
| Decision benchmark, 96 scenarios | 54 s | Runs on every change as part of `pytest`. |
| Python drill, 4 changes | 2 min 30 s | pytest, coverage.py and mutmut, including tool start-up. |
| TypeScript drill, 3 changes | 64 s | Vitest and StrykerJS. |
| Go drill, 3 changes | 58 s | `go test` and gremlins. |

**Mutation testing.** Its cost follows the size of the change, not the size of the repository.
The gate mutates only the changed functions, plus the functions a changed test names. The three
L4 scorecard controls added 10 functions, and the gate had 185 mutants to run.

**Developer time.** The cost a gate imposes on a team is mostly its false blocks: each one is a
dispute, a waiver request or a weakened test.

- The benchmark's 38 legitimate pull requests produce 0 false blocks. v1.0.0 got 20 of them wrong,
  including Django's `tests.py`, relative imports and Angular file names.
- The one known gap errs on the permissive side and leaves the backstop to mutation testing.

Real CI cost on this branch will come from its first pull-request run. The only runs so far are
v1.0.0's.

## 5. It gates itself

This repository runs on its own pipeline, and its own gates changed its own code during
development:

- **A change never grades itself.**
  - `a173861` added a `scripts/**` exemption to `golden.toml` so the drill harness needn't ship
    tests.
  - The base-anchored policy (`425cad3`) ignores an exemption the pull request adds for itself,
    so test presence still failed.
  - The fix was tests, not the exemption: `a5bb49c` added `tests/test_drill.py`, put `scripts/`
    and `evals/` under coverage, and removed the exemption.
- **Fail-closed on its own tooling.**
  - Running the benchmark under mutmut crashed mutmut's statistics run, because eval
    subprocesses inherited `MUTANT_UNDER_TEST`.
  - The runner now scrubs it (`97ec171`).
  - The gate refused the crashed report as "not a pass" instead of reading it as "no mutants".
- **The benchmark caught an escape in a new gate before it merged.**
  - The first version of the arid-line mutant filter scored 100% on mutation testing of its own
    functions.
  - The benchmark still found that `console.log(applyDiscount(x > 100))` hid the mutants of the
    call inside it, so the mutation gate passed.
  - The filter was tightened before merge (`99ebf4a`), and the scenario now guards it. Unit
    tests check that code does what its author meant; only decision-level ground truth catches a
    gate that is wrong by design.
- **The red-team review** ([2026-10-22](redteam/2026-10-22-review.md)) found 18 issues by
  attacking the pipeline as an adopter would. All 18 are fixed.
  - 13 have a benchmark scenario or a drill that fails if the fix regresses.
  - The other 5 are covered elsewhere:
    - The owner-review control needs the GitHub API, so it has unit tests.
    - Three are workflow changes, checked by actionlint and zizmor.
    - One is a documentation fix, checked by the alignment test.
  - One example: `release.yml` accepted any check run named `ci`, which any GitHub App can create
    (`3ebe7c1`).

## 6. What this does not prove

- **The benchmark was written by the people who fixed the gates.** It can be taught to.
  - Mitigation 1: every scenario was run against the old code first, and a scenario that passed
    before its fix was rejected as proof (see [Adding a scenario](../evals/README.md#adding-a-scenario)).
  - Mitigation 2: the real-fault benchmark takes its ground truth from other projects' bug
    trackers, not from us.
- **No production outcome data yet.** The value that matters most is fewer escaped defects and
  shorter lead times in adopting teams.
  - `golden dora` computes all five DORA metrics from a deployment log.
  - [Evaluation §5](evaluation.md) describes the staged rollout that measures them before and
    after adoption.
  - Until adopters report those numbers, this page measures the gates' decisions, not their
    business effect.
- **Mutation testing is a proxy for fault detection.** The coupling evidence is strong but not
  absolute:
  - [Just et al., FSE 2014](https://homes.cs.washington.edu/~mernst/pubs/mutation-effectiveness-fse2014-abstract.html)
    found mutants coupled to 73% of real faults.
  - [Google ICSE 2021](https://arxiv.org/abs/2103.07189) found them coupled to 70% of
    high-priority bugs.
  - The real-fault benchmark measures it for this pipeline's own configuration, so far on a
    small sample (section 3).
