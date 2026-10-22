# Evaluation: how to know the pipeline is actually golden

Evaluate at three levels. Each answers a different question, and each alone is misleading:

| Level | Question | Method | Tooling |
|---|---|---|---|
| **1. Capability** | Are the right controls in place, and can they be bypassed? | maturity scorecard on every run | `golden scorecard`, OpenSSF Scorecard |
| **2. Effectiveness** | Do the gates catch the defects they claim to? | fault-injection drills, mutation score, escaped-defect review | `scripts/drill.py`, `golden mutation` |
| **3. Outcomes** | Is delivery faster *and* more stable? | DORA metrics plus pipeline health | `golden dora`, Actions API |

Capability without effectiveness is compliance theatre. Outcomes without the other two can't tell
you *why* something got better or worse.

## 1. Capability: the maturity scorecard

`golden scorecard` runs inside the `gates` job of every pipeline run (see
[gates](gates.md#repository-scorecard) for the controls) and fails below `scorecard.min_level`.

| Level | Meaning | Typical blocker |
|---|---|---|
| L1 Adopted | the pipeline runs | — |
| L2 Owned | someone is accountable; updates arrive automatically | missing CODEOWNERS or SECURITY.md |
| L3 Enforced | the gate cannot be bypassed and the supply chain is pinned | ruleset not requiring `ci / golden`; unpinned third-party actions |
| L4 Verified | releases are signed; no exceptions outstanding | active waiver; no provenance |

The scorecard runs on every PR, so a change that deletes a gate (say, removing the ruleset
requirement or unpinning an action) lowers the level and fails *that* PR. The gates protect
themselves. Recommended targets: **L2** to adopt, **L3** within one quarter, **L4** for anything
that ships to production.

Complement it with the OpenSSF Scorecard (`.github/workflows/scorecard.yml` in this repo).
Targets: 10/10 on Token-Permissions, Pinned-Dependencies, Dangerous-Workflow, SAST,
Dependency-Update-Tool and Security-Policy; 8 or higher on Branch-Protection and Code-Review.

## 2. Effectiveness: do the gates catch defects?

### 2.1 Metrics

| Metric | Definition | Target | Why |
|---|---|---|---|
| Diff coverage | % of changed coverable lines executed by tests | ≥ 90% (floor 80) | Google's "exemplary" level; per-change, so it doesn't punish legacy code |
| Mutation score (changed code) | (killed + timeout) / valid mutants | ≥ 80% (floor 60) | coverage is weakly correlated with fault detection; mutants are coupled to 70–73% of real faults |
| Flake rate | runs with inconsistent results on the same commit / all runs | < 0.5% | Google measured 1.5%; tests lose value approaching 1% |
| Escaped-defect rate | production defects a gate should have caught / all production defects | falling quarter over quarter | the ground truth for every other metric |
| Gate false-positive rate | gate failures judged wrong by the author and confirmed by a reviewer / all gate failures | < 5% | above that, people learn to route around the gate |
| Waiver count and age | active waivers, days to expiry | trending to 0 | waivers are debt with a due date |

### 2.2 Escaped-defect review

For every production incident or customer-found bug, answer in the post-incident review: *which
gate should have caught this, and why didn't it?* Then do one of the following:

- add the missing test, and with it a new drill scenario that reproduces the escape
- tighten a threshold in `golden.toml`
- propose a new control

This feedback loop is how the gates keep pace with how the code actually fails.

## 3. Drills: does each gate catch what it claims?

`scripts/drill.py` is the pipeline's own fault-injection test. For each language it does the
following:

1. Copies the example project into a scratch repository, inside a `services/<language>/`
   subdirectory to mimic a monorepo.
2. Adds the same small feature, a tiered bulk discount, on a branch.
3. Writes it in three ways and runs the real gate commands on each:

| Scenario | What the author did |
|---|---|
| **no-tests** | shipped the feature without tests |
| **weak** | wrote a test that *executes* every line but asserts only that the result exists |
| **strong** | wrote boundary-value tests (1, 9, 10, 49, 50) |

The expected verdicts are asserted, and the drills run in this repository's CI on every change.
Results on 2026-10-21 (local run, 56 s for all nine):

| Language | Scenario | Diff coverage | Test presence | Mutation | As expected |
|---|---|---|---|---|---|
| python | no-tests | **block** | **block** | **block** | ✅ |
| python | weak | pass | pass | **block** | ✅ |
| python | strong | pass | pass | pass | ✅ |
| node | no-tests | **block** | **block** | **block** | ✅ |
| node | weak | pass | pass | **block** | ✅ |
| node | strong | pass | pass | pass | ✅ |
| go | no-tests | **block** | **block** | **block** | ✅ |
| go | weak | pass | pass | **block** | ✅ |
| go | strong | pass | pass | pass | ✅ |

**The weak-test row is the case for this whole design.** A test that runs every line and asserts
nothing gets 100% diff coverage and satisfies test presence. Only mutation testing exposes it.

### What the drills found while this pipeline was being built

The drills found defects in the pipeline itself during development. Each one was fixed, and each
now has a regression test:

| Finding | Effect before the fix | Fix |
|---|---|---|
| Mutation scoped to whole *modules* | Python weak-test drill scored 86% (43 mutants killed elsewhere in the module, 7 unkilled in the new function) and **passed** | scope mutmut to changed *functions*, Stryker to changed *line ranges*, gremlins via line filtering |
| gremlins `--diff` in a subdirectory | skipped every mutant; would read as "no mutants" | mutate the module, then filter by changed lines in `golden mutation --base` |
| gremlins default timeout | 20 of 25 mutants "timed out"; under Stryker's formula a broken run scored 80% | `--timeout-coefficient 10`, and fail any run where most mutants time out |
| Missing mutation report | Stryker crashed on TypeScript 7; the gate printed "no mutants" and passed | missing report is an error (fail closed) |
| Stryker + Vitest 5 | ran 0 tests per mutant; every mutant "survived" | example pinned to Vitest 4.1 / TypeScript 6.0; documented |
| Test-presence annotations in monorepos | annotation paths relative to the project, so GitHub attached them to the wrong file | rewrite to repository-relative paths |

### Mutation testing the gates themselves

The CLI's own code is held to the bar it enforces: `mutmut` over `src/golden` scores **81.4%**
(1,883 killed, 3 timed out, 432 survived). Surviving mutants cluster in argparse help strings and
report formatting, which are arid code by Google's definition. The patterns excluded for that
reason are listed and justified in `pyproject.toml`.

## 4. Outcomes: DORA and pipeline health

Record one JSON line per production deployment, for example from your deploy job:

```json
{"sha": "…", "committed_at": "2026-10-01T08:00:00+00:00", "deployed_at": "2026-10-01T10:00:00+00:00", "failed": false, "restored_at": null}
```

Then run:

```console
$ golden dora deployments.jsonl
{"deploys_per_day": 1.0, "lead_time_hours_p50": 1.5, "change_failure_rate": 0.25, "time_to_restore_hours_p50": 1.0}
```

| DORA metric (2024+) | Elite reference (DORA 2024) |
|---|---|
| Deployment frequency | on demand |
| Change lead time | < 1 day |
| Change fail rate | ~5% |
| Failed deployment recovery time | < 1 hour |
| Deployment rework rate | track the trend (new in 2024) |

Pipeline health (Actions API: `GET /repos/{repo}/actions/runs`, `…/jobs`):

| Metric | Target | Why |
|---|---|---|
| PR pipeline duration p50 / p95 | < 10 / < 20 min | slow gates produce large PRs, and large PRs produce defects |
| Mutation job duration p95 | < 10 min | if exceeded, add arid patterns or exclusions before lowering the bar |
| Time `main` is red | < 1% | the "always green mainline" promise |
| Merge-queue wait p95 | < 15 min | long queues invite bypass requests |
| Ruleset bypasses | 0 | every bypass gets a review |

Read the outcome metrics together. The 2025 DORA report found AI-assisted teams gaining
throughput while *losing* stability. Rising deploy frequency with a rising change-fail rate means
the gates are not keeping up.
