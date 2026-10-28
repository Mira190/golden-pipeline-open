# Research: how large engineering organisations ship without a manual QA gate

The organisations that ship fastest didn't drop testing. They turned every QA question into a
gate that runs automatically on every change, put the results inside code review, and then
measured the gates themselves. This page collects the evidence behind each design decision in
this repository. Figures are taken from the primary sources where possible; **(2°)** marks a
figure confirmed only through secondary coverage.

## 1. Test the change, not the codebase: diff coverage

**Google, "Code Coverage at Google"** (Ivanković, Petrović, Just, Fraser; ESEC/FSE 2019, doi
10.1145/3338906.3340459):

- Coverage is computed for about 1B lines daily across 7 languages, automatically for every
  changelist, and shown in code review (Critique).
- "Google does not enforce any code coverage thresholds across the entire codebase." Projects
  instead opt into levels. Levels 3–5 require project/changelist coverage of 60/70%, 75/80% and
  90/90%.
- 512 surveyed developers: 45% use coverage often when authoring a change, and 40% when
  reviewing one.

**Google Testing Blog, "Code Coverage Best Practices"** (Arguelles, Ivanković, Bender; Aug 2020)
is the source of the widely quoted guidance "60% acceptable, 75% commendable, 90% exemplary".
Those labels are not from the FSE paper.

**Hilton, Bell, Marinov, "A Large-Scale Study of Test Coverage Evolution"** (ASE 2018), 7,816 builds
of 47 projects:

- Patch coverage does not correlate with overall coverage (Kendall τ ≈ −0.01).
- Patches often change the coverage of code they didn't touch.

→ **Design:** gate *changed lines* at 90% (floor 80). Also run a *total-coverage ratchet*, because
a patch can silently lower coverage elsewhere. There is deliberately no global threshold.

## 2. Coverage is necessary, not sufficient: mutation testing

**Inozemtseva & Holmes, "Coverage is not strongly correlated with test suite effectiveness"**
(ICSE 2014): 31,000 suites over 5 Java systems. Once suite size is controlled, the correlation
is low to moderate.

**Just et al., "Are mutants a valid substitute for real faults in software testing?"** (FSE 2014):
357 real faults and 230K mutants. 73% of real faults are coupled to mutants, and mutant
detection correlates with real-fault detection more strongly than statement coverage does.

**Google mutation testing**

- **"State of Mutation Testing at Google"** (Petrović & Ivanković, ICSE-SEIP 2018): mutants are
  generated only for diffs and surfaced in code review.
- **"Practical Mutation Testing at Scale: A view from Google"** (Petrović, Ivanković, Fraser, Just;
  IEEE TSE 2021, arXiv 2102.11378):
  - Developers initially rated 85% of mutants unproductive.
  - Suppressing *arid nodes* (logging, configuration and other code no test should pin down)
    raised productive mutants from about **15% to about 80%**; later refinements reached
    **89%**. Separately, the system generates at most one mutant per line and caps mutants per
    changelist (7× the number of files).
  - Used by more than 24,000 developers on more than 1,000 projects.
  - Surviving mutants are *code-review findings*: an author need not resolve them before
    submitting unless a reviewer marks them mandatory. This pipeline deliberately blocks on a
    score instead, because a GitHub pull request has no "reviewer marks a finding mandatory"
    step that a gate could enforce. The threshold is kept below 100% and waivable for that
    reason.
- **"Does mutation testing improve testing practices?"** (ICSE 2021, arXiv 2103.07189): developers
  shown mutants write more tests, and mutants were coupled to **70%** (1,043 of 1,502) of
  high-priority real bugs on the change that introduced them.

**A caveat worth stating.** Papadakis et al., "Are Mutation Scores Correlated with Real Fault
Detection?" (ICSE 2018) found the correlation between mutation score and real-fault detection
*weak* once test-suite size is controlled for. Suites with higher mutation scores still detected
significantly more real faults than random suites of the same size. Mutation score is therefore a
floor that blocks hollow tests, not a proof of quality. That's one reason the gate threshold
(80%) is well below 100%, and why the [evaluation guide](evaluation.md) recommends replaying
real faults as well as mutants.

→ **Design:**

- Mutation testing runs on pull requests and is **scoped to the changed lines or functions**,
  never the whole module. Our own drills showed a module-wide score hides an untested new
  function ([evaluation](evaluation.md#3-drills-does-each-gate-catch-what-it-claims)).
- Arid lines are suppressed explicitly, with each pattern documented.
- The score uses Stryker's definition, (killed + timeout) / (killed + timeout + survived + no coverage),
  for every language.

## 3. Someone must have written a test: test presence

There is no direct industry paper for this gate. It encodes the "Beyoncé Rule" from
*Software Engineering at Google* (Winters, Manshreck, Wright; O'Reilly 2020, ch. 11): "If you liked
it, then you shoulda put a test on it." Diff coverage alone is satisfied when an old,
coarse-grained test happens to execute the new code. Test presence requires a *changed* test that
references the changed file. A PR label switches it to report-only (for refactors), and that is
visible to reviewers.

## 4. Flaky tests destroy trust in every other gate

- **Google**
  - "Flaky Tests at Google and How We Mitigate Them" (J. Micco, Google Testing Blog, 2016):
    about **1.5% of all test runs** were flaky, and **almost 16% of tests** showed some
    flakiness (2°, figures as of 2016).
  - *SWE at Google* (2020), ch. 11: as a suite approaches 1% flakiness, its tests "begin to lose
    value", and "at Google, our flaky rate hovers around **0.15%**". That is a tenth of the 2016
    figure, after years of investment.
- **Luo et al., "An empirical analysis of flaky tests"** (FSE 2014): the leading root causes are
  async wait (45%), concurrency (20%) and test-order dependency (12%). **78% of flaky tests are
  flaky when first written.**
- **Meta, "Probabilistic Flakiness"** (engineering blog, 2020): "all real-world tests are flaky to
  some degree", so flakiness should be measured continuously.

→ **Design:**

- Tests run in **random order** in every language: pytest-randomly, `--sequence.shuffle`,
  `-shuffle=on`.
- **New and changed tests are re-run 10×** before merge, because that is when most flakiness is
  born.
- There is no retry-until-green anywhere.

## 5. Findings belong in code review, on the diff

**Distefano, Fähndrich, Logozzo, O'Hearn, "Scaling Static Analyses at Facebook"** (CACM 62(8),
2019): the same Infer analysis had a near-0% fix rate when shipped as offline bug lists, and over
70% when reported on the diff at review time.

→ **Design:** every gate emits GitHub annotations on the changed line (uncovered line, surviving
mutant, missing test) and a markdown verdict in the job summary.

## 6. Keep the mainline green

- **Uber SubmitQueue**, "Keeping Master Green at Scale" (Ananthanarayanan et al., EuroSys 2019):
  over one week without it, the iOS mainline was green only 52% of the time (§8.5). Speculative
  builds keep master always green.
- **Shopify Merge Queue v2** (2019): batched, speculative merges serving more than 1,000
  developers.
- **Google TAP** (*SWE at Google*, ch. 23): a change that passes presubmit has a 95%+ chance of
  passing everything else.

→ **Design:** workflows support `merge_group`, so diff-based gates evaluate the merge-queue
commit against its base. A single required check (`<job> / golden`) aggregates every job.

## 7. LLM-generated tests are useful only behind objective filters

- **Meta TestGen-LLM**, "Automated Unit Test Improvement using Large Language Models at Meta"
  (Alshahwan et al., FSE 2024 Industry, arXiv 2402.09171):
  - Of generated tests, 75% built, 57% passed reliably and 25% increased coverage.
  - 73% of recommendations were accepted for production.
  - Only tests that cleared the filters reached engineers.
- **Meta ACH**, "Mutation-Guided LLM-based Test Generation at Meta" (Foster et al., FSE Companion
  2025, arXiv 2501.12862): 73% of generated tests accepted, and 36% judged privacy-relevant.

→ **Design:** the gates are tool-agnostic, so a test written by a person or by an AI assistant
must pass the same filters: build, pass 10 runs, cover the changed lines and kill the mutants.
That is exactly Meta's "assured" filter.

## 8. Progressive delivery replaces the final manual sign-off

- **Amazon**, "Automating safe, hands-off deployments" (C. Liguori, Builders' Library):
  - one-box first, then waves
  - bake times of at least 1 hour after one-box and at least 12 hours after the first regional
    wave
  - automatic rollback on aggregate alarms
  - "code review is the last manual review and approval"
- **Google SRE Workbook**, ch. 16 "Canarying Releases": compare canary against a control
  population, use a small number of SLI-based metrics, and run one canary at a time.
- **Netflix/Google Kayenta**: automated canary judgement with a Mann–Whitney U test.
- **CrowdStrike, 19 July 2024** (Preliminary PIR and RCA): a content update that bypassed staged
  rollout crashed about 8.5M Windows hosts (Microsoft's estimate). The remediation was staggered
  deployment rings for *all* change types.

→ **Design:** `container.yml` produces a signed, attested image. The provenance can be verified
at deploy time (`gh attestation verify --signer-workflow`), which is the hook for a progressive
rollout controller. Rollout itself is environment-specific and out of scope here; see
[adoption](adoption.md#progressive-delivery).

## 9. Measure delivery, not activity: DORA

- *Accelerate* (Forsgren, Humble, Kim; 2018) and the DORA programme ([dora.dev](https://dora.dev/guides/dora-metrics/))
  currently use **five** metrics:
  - **Throughput:** change lead time, deployment frequency, failed deployment recovery time.
  - **Instability:** change fail rate and deployment rework rate. Rework rate was added in 2024.
- The 2025 *State of AI-assisted Software Development* report (about 5,000 respondents) found that
  AI adoption now correlates with higher throughput but still with *lower* stability. Strong
  version-control practice, small batches and quality internal platforms are among the
  capabilities it associates with turning AI adoption into good outcomes.

→ **Design:** `golden dora` computes the delivery metrics from a deployment log, and the
[evaluation guide](evaluation.md) pairs them with gate-effectiveness metrics.

## 10. A paved road, hardened

- **Spotify**, "How We Use Golden Paths…" (2020): one opinionated, supported path instead of
  "rumour-driven development".
- **Supply-chain incidents of 2025–2026**, all of which reached victims *through CI*:
  - tj-actions/changed-files (CVE-2025-30066)
  - nx "s1ngularity"
  - Shai-Hulud and Sha1-Hulud 2.0
  - Trivy / TeamPCP (CVE-2026-33634)
  - TanStack's cache-poisoning compromise (May 2026)

  The [security model](security-model.md) maps each one to the control that addresses it.

## 11. A change never grades itself

Separation of duties is the oldest control in change management. The supply-chain frameworks
restate it for CI:

- **SLSA** v1.2's source track requires two-party review at its highest level (Source L4).
  Lower levels require controls and history, not a second reviewer.
- **GitHub** offers organisation rulesets that *require a workflow* from a central repository, so
  a pull request cannot edit the check that judges it.

v1.0.0 of this repository broke the rule in two places. The decision benchmark
([`evals/`](../evals/README.md)) found the first, and a red-team review found the second:

- The gates read `golden.toml` from the branch under review, so a pull request could lower its
  own thresholds or grant itself a waiver.
- On `pull_request` events GitHub runs the *pull request's* copy of the caller workflow.

→ **Design:**

- Every gate reads the policy at the pull request's base commit. The change may tighten it;
  loosening applies after merge.
- The scorecard's L3 requires code-owner review of `.github/workflows/` and `golden.toml`, and
  fails any caller that switches a gate off.
- Organisation-wide, require the workflow through a ruleset. See
  [adoption](adoption.md) and the [security model](security-model.md).

## 12. Related projects: what is reused, adapted, or left out

Every gate here has prior art. Where a mature tool exists, the pipeline calls it rather than
re-implementing it. The `golden` CLI only adds the parts that are missing: diff scoping across
tools, one score definition, fail-closed handling and policy.

| Concern | Prior art | Used here as | Notes |
|---|---|---|---|
| Diff coverage | [diff-cover](https://github.com/Bachmann1234/diff_cover); Codecov/Coveralls patch status; SonarQube "Clean as You Code" (80% on new code) | **reused**: diff-cover computes the number; `golden` adds fail-closed handling of unmeasured files and the policy floor | SonarQube's Community Build ignores coverage on changes under 20 lines (its "fudge factor"). Not adopted, because a small change to billing code deserves a test too. |
| Diff-scoped mutation | Google's diff-based mutation service; [cargo-mutants `--in-diff`](https://mutants.rs/in-diff.html); StrykerJS `--mutate file:lines` and `--incremental`; PIT with git integration | **adapted**: the same "mutants overlapping the diff" rule, implemented once for mutmut (functions), Stryker (line ranges) and gremlins (filtered report) | cargo-mutants warns that a diff-scoped run is no substitute for an occasional full run. Run a scheduled full mutation pass (for example weekly) to find tests weakened outside the diff. |
| Mutation report format | [mutation-testing-elements](https://github.com/stryker-mutator/mutation-testing-elements) schema (StrykerJS, Stryker.NET, Stryker4s, muttest) | **reused**: `--format mutation-testing-elements` reads any compliant report | Makes the mutation gate usable for .NET and Scala without new code. |
| "Did they add a test?" | [Danger](https://danger.systems) rules ("app files changed, no test files changed"), GitLab's Danger roulette | **adapted** into a blocking gate, with import resolution instead of "path contains `test`" | Danger rules warn; here the PR label is the escape hatch, and the evals check that gaming it (comments, unrelated tests) fails. |
| Flaky tests | pytest-flakefinder, pytest-repeat, DeFlaker / pytest-flakefighters, iDFlakies, Mergify's "rerun new tests" | **adapted**: re-run *changed* runnable tests N× (cheapest point, Luo 2014) | DeFlaker-style classification (a failing test that covers no changed line is probably flaky) would cut false blocks on unrelated failures. It is a candidate for a later version. |
| Repository maturity | [OpenSSF Scorecard](https://github.com/ossf/scorecard), Allstar, Legitify, safe-settings | **complemented**: `golden scorecard` checks the controls that keep *these* gates un-bypassable; OpenSSF Scorecard runs alongside | "Unknown never counts as a pass" is borrowed from Scorecard's treatment of missing API access. |
| Expiring exceptions | OSV-Scanner `[[IgnoredVulns]] ignoreUntil / reason` | **same model** as `[waivers.<gate>]` (reason, at most 90 days) | Use OSV-Scanner's file for vulnerability exceptions, so one review pattern covers both. |
| Delivery metrics | [dora-team/fourkeys](https://github.com/dora-team/fourkeys) (archived), Apache DevLake | **minimal re-implementation**: `golden dora` computes the five metrics from a JSON Lines log, offline | Use DevLake when you want dashboards and incident joins. `golden dora` is for a quick, scriptable check. |
| Workflow security | zizmor, actionlint + shellcheck, poutine, harden-runner | **reused** in `security.yml` | — |
| Real-fault benchmarks | Defects4J (Java), BugsInPy (Python) | **recommended** for the next evaluation layer | See [evaluation](evaluation.md#0-evaluating-the-evaluator-how-this-repository-is-tested). |

## Principles, and where they live in this repository

| # | Principle | Evidence | Implementation |
|---|---|---|---|
| 1 | Gate the change, not the codebase | FSE 2019; Hilton 2018 | `golden diff-coverage`, `golden ratchet` |
| 2 | Coverage ≠ effectiveness; add diff-scoped mutation with arid filtering | Inozemtseva 2014; Just 2014; Google TSE 2021, ICSE 2021 | `golden mutation --base`, line/function-scoped targets |
| 3 | Every change ships a test | SWE@Google ch. 11 | `golden presence` |
| 4 | Randomise order; reject new flakes before merge | Google 2016; Luo 2014 | shuffled runners, 10× flake gate |
| 5 | Report on the diff | Facebook CACM 2019 | annotations + job summary |
| 6 | Mainline always green | SubmitQueue 2019; TAP | `merge_group`, single required check |
| 7 | Generated tests only behind objective filters | TestGen-LLM 2024; ACH 2025 | the same gates for every author |
| 8 | Progressive, verifiable delivery; no exempt change types | Amazon; SRE Workbook; CrowdStrike RCA | `container.yml`, signed provenance |
| 9 | Small batches, measured with DORA | DORA 2024/2025 | `golden dora`, evaluation guide |
| 10 | One maintained paved road with supply-chain hardening | Spotify; 2025–26 incidents | reusable workflows, pinned, least privilege |
| 11 | A change never grades itself | separation of duties; SLSA Source L4 two-party review; required workflows | base-anchored `golden.toml`, scorecard `owner-review`, `gates-on` and `fresh-review`, plan-time warning on self-modifying PRs |
| 12 | Measure the gates' decisions, not just their code | drills; escaped-defect review | `evals/` decision benchmark, `scripts/drill.py` |
