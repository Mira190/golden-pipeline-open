# Alignment with large-organisation practice

This page maps the published engineering practices of large organisations and industry standards
to this pipeline's controls. Each row has three parts:

- **Practice and source.** What the organisation or standard does, with a link to the primary
  source.
- **Control here.** The gate, check or workflow that implements it.
- **Evidence.** Executable proof. Every token in this column is checked by
  `tests/test_alignment.py`; if a cited scenario, test, job or control disappears, the test
  suite fails.

| Token | Points to |
|---|---|
| `scenario:<id>` | a labelled pull request in the [decision benchmark](../evals/README.md), replayed through the real CLI on every change |
| `drill:<language>/<name>` | an end-to-end drill with the real toolchains (`scripts/drill.py`) |
| `realfault:<id>` | a real bug from an open-source project in the [real-fault benchmark](../evals/realfaults/README.md) |
| `test:<file>::<function>` | a unit test |
| `job:<workflow>#<job>`, `file:<path>` | a workflow job or file |
| `control:<id>`, `cmd:<subcommand>` | a scorecard control, a `golden` command |

Status: ✅ implemented, 🟡 partial (the gap is stated), ⛔ deliberately not adopted (the reason is
stated).

## A. Testing replaces the manual QA gate

| # | Practice | Source | Control here | Evidence | Status |
|---|---|---|---|---|---|
| 1 | Measure coverage of the *change* on every code review; ~90% is "exemplary". There is no global threshold. | Google: [Ivanković et al., FSE 2019](https://research.google/pubs/code-coverage-at-google/); [Testing Blog 2020](https://testing.googleblog.com/2020/08/code-coverage-best-practices.html) | Diff coverage ≥ 90% (floor 80) | `cmd:diff-coverage` `scenario:diff-coverage/uncovered` `scenario:diff-coverage/covered` `drill:python/no-tests` `test:tests/test_cli.py::test_diff_coverage_fails_on_uncovered_lines` | ✅ |
| 2 | A change can lower coverage of code it didn't touch, so watch the total too. | [Hilton et al., ASE 2018](https://dl.acm.org/doi/10.1145/3238147.3238183) | Coverage ratchet against the default branch | `cmd:ratchet` `scenario:ratchet/drop-blocks` `scenario:ratchet/tolerance-loosened-in-pr` | ✅ |
| 3 | An untested new file must not read as "100% covered": coverage is computed for every file in the change. | [Google FSE 2019](https://research.google/pubs/code-coverage-at-google/) | Diff coverage fails on changed files missing from the report | `test:tests/test_cli.py::test_diff_coverage_fails_on_a_module_missing_from_the_report` | ✅ |
| 4 | Mutation testing on the diff, surfaced at review time, because coverage says nothing about assertions. | Google: [Petrović & Ivanković, ICSE-SEIP 2018](https://research.google/pubs/state-of-mutation-testing-at-google/); [TSE 2021](https://arxiv.org/abs/2102.11378) | Diff-scoped mutation gate ≥ 80% (floor 60), for mutmut, StrykerJS, gremlins and any mutation-testing-elements report | `cmd:mutation` `drill:python/weak` `drill:node/weak` `drill:go/weak` `scenario:mutation/stryker-survivor-blocks` `scenario:mutation-targets/python-functions` | ✅ |
| 5 | Mutants on the code a change puts at stake are coupled to the real bugs that change introduces. | [Google ICSE 2021](https://arxiv.org/abs/2103.07189) (70% of high-priority bugs); [Just et al., FSE 2014](https://homes.cs.washington.edu/~mernst/pubs/mutation-effectiveness-fse2014-abstract.html) | Edits to tests put the functions they name in mutation scope, so a test-only weakening is caught | `scenario:mutation-targets/test-only-weakening` `scenario:mutation-targets/test-file-deleted` `scenario:mutation/go-test-weakening-blocks` `test:tests/test_impact.py::test_mutation_lines_add_functions_a_weakened_test_names` | ✅ |
| 6 | "If you liked it, then you shoulda put a test on it": every change ships a test. | [*Software Engineering at Google*, ch. 11](https://abseil.io/resources/swe-book/html/ch11.html) | Test presence: a changed test must reference each changed source file | `cmd:presence` `scenario:presence/python-no-tests` `scenario:presence/python-comment-mention` `drill:python/no-tests` | ✅ |
| 7 | Flaky tests destroy trust. Run in random order and catch new flakes when they are written (78% are flaky from the start). | [SWE at Google ch. 11](https://abseil.io/resources/swe-book/html/ch11.html); [Luo et al., FSE 2014](https://dl.acm.org/doi/10.1145/2635868.2635920) | Random order in every runner; changed runnable tests re-run 10× before the merge; after it, a nightly scan runs the whole suite N times and reports flaky and always-failing tests | `job:golden.yml#gates` `scenario:flake/leaked-state` `scenario:flake/conftest-only` `cmd:flaky-scan` `job:scheduled.yml#scan` `file:templates/ci-scheduled.yml` | ✅ |
| 8 | Generated tests are accepted only through objective filters: they must build, pass reliably and improve coverage. | Meta: [TestGen-LLM, FSE 2024](https://arxiv.org/abs/2402.09171); [ACH, 2025](https://arxiv.org/abs/2501.12862) | The same gates apply to every author, human or AI | `drill:python/weak` `drill:node/weak` `scenario:presence/python-unrelated-test` | ✅ |
| 9 | Static-analysis findings go on the diff at review time (fix rate over 70%, against near 0% for offline lists). | Meta: [Distefano et al., CACM 2019](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/); Google: [SWE at Google ch. 20](https://abseil.io/resources/swe-book/html/ch20.html) | Every gate annotates the changed line. `golden sast` blocks only on the static-analysis findings a change introduces, from any SARIF tool (`sast.yml` runs CodeQL). The `code-scanning` control (L4) requires CodeQL or Semgrep on pull requests | `cmd:sast` `file:.github/workflows/sast.yml` `test:tests/test_sarif.py::test_findings_in_untouched_code_never_block` `test:tests/test_sarif.py::test_codeql_keeps_rule_severities_in_extensions` `control:code-scanning` `scenario:presence/annotation-path-escaping` `test:tests/test_cli.py::test_diff_coverage_fails_on_uncovered_lines` | ✅ |
| 10 | A broken tool must never read as a pass: gates fail closed. | This repository's drills ([evaluation §3](evaluation.md#3-drills-does-each-gate-catch-what-it-claims)) | A missing, empty, truncated or malformed report or policy is an error | `scenario:mutation/missing-report` `scenario:mutation/truncated-report` `scenario:ratchet/corrupt-report` `scenario:policy/typo-key` `scenario:policy/nan-threshold` | ✅ |

## B. Code review and separation of duties

| # | Practice | Source | Control here | Evidence | Status |
|---|---|---|---|---|---|
| 11 | Quality bars are set centrally with floors; teams may opt into stricter levels. | [Google FSE 2019](https://research.google/pubs/code-coverage-at-google/) (opt-in coverage levels) | `golden.toml` with platform floors | `cmd:policy` `scenario:policy/below-floor` `scenario:policy/valid-stricter` | ✅ |
| 12 | An exception is visible, justified and time-boxed. | OSV-Scanner [`ignoreUntil`](https://google.github.io/osv-scanner/configuration/) | Waivers need a reason and expire within 90 days; any active waiver blocks L4 | `test:tests/test_config.py::test_waiver_active_until_expiry` `scenario:presence/python-base-waiver-honoured` `control:waivers` | ✅ |
| 13 | A change never grades itself (separation of duties). | [SLSA v1.2 source track](https://slsa.dev/spec/v1.2/source-requirements) | The policy is read at the base commit; a PR may tighten it but never loosen, waive or exempt itself | `scenario:policy/pr-loosens-its-own-bar` `scenario:presence/python-waiver-added-in-pr` `scenario:presence/python-exempt-added-in-pr` `test:tests/test_config.py::test_merge_tightens_but_never_loosens` | ✅ |
| 14 | Changes to the pipeline and its policy need an owner's review. Required workflows run from a central repository. | [GitHub rulesets: Require workflows to pass](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) | `owner-review` and `gates-on` controls; `golden plan` flags a PR that edits its own gating | `control:owner-review` `control:gates-on` `scenario:plan/flags-a-change-to-its-own-workflow` `scenario:scorecard/gate-disabled-in-caller` | 🟡 The ruleset itself is a setting the scorecard verifies, not code |
| 15 | Two-person review of the *final* code. | [SLSA v1.2 Source L4](https://slsa.dev/spec/v1.2/source-requirements) (two-party review); [Google code review](https://google.github.io/eng-practices/review/) | `reviews` (L3) and `fresh-review` (L4: approvals dismissed on push, last pusher can't self-approve). This matches the Source L4 requirement; no Source level is claimed (row 35) | `control:reviews` `control:fresh-review` `test:tests/test_scorecard.py::test_fresh_review` | ✅ |

## C. Mainline health and delivery

| # | Practice | Source | Control here | Evidence | Status |
|---|---|---|---|---|---|
| 16 | Keep the mainline green under concurrent merges, with one required verdict. | Uber: [SubmitQueue, EuroSys 2019](https://www.masoud.io/docs/eurosys19.pdf); [Shopify merge queue](https://shopify.engineering/successfully-merging-work-1000-developers) | `merge_group` support; the single `golden` check aggregates every job; `required-check` (L3) and `merge-queue` (L4) controls | `job:golden.yml#golden` `control:required-check` `control:merge-queue` `test:tests/test_plan.py::test_event_base` | ✅ |
| 17 | Presubmit: the same checks before code is shared. | [SWE at Google ch. 23](https://abseil.io/resources/swe-book/html/ch23.html); [DORA continuous integration](https://dora.dev/capabilities/continuous-integration/) | `golden local` runs the pipeline's commands and gates on a laptop or in an agent's loop | `cmd:local` `test:tests/test_local.py::test_full_run_mirrors_the_pipeline` | ✅ |
| 18 | Test what ships: build once, or re-verify the artifact you publish. | [continuousdelivery.com](https://continuousdelivery.com/implementing/patterns/); [Google SRE, release engineering](https://sre.google/sre-book/release-engineering/) | The published digest is smoke-tested and scanned before it is attested | `job:container.yml#publish` | ✅ |
| 19 | Release only reviewed, tested code from the main line. | [Amazon Builders' Library](https://aws.amazon.com/builders-library/automating-safe-hands-off-deployments/) | `release.yml`: the tag must be on main, with a green `ci` check run from GitHub Actions. The `release-tags` control (L4) requires a tag ruleset, so a released tag can't be moved or deleted ([SLSA v1.2 source](https://slsa.dev/spec/v1.2/source-requirements): tags "MUST be configured to prevent them from being moved or deleted") | `job:release.yml#release` `control:release-tags` | ✅ |

## D. Supply chain

| # | Practice | Source | Control here | Evidence | Status |
|---|---|---|---|---|---|
| 20 | Pin every dependency, including the build and gate tooling (hermetic builds). | [OpenSSF Scorecard: Pinned-Dependencies](https://github.com/ossf/scorecard/blob/main/docs/checks.md#pinned-dependencies); [Google SRE ch. 8](https://sre.google/sre-book/release-engineering/) | Actions pinned by SHA; the CLI installed from `uv.lock` with hashes; build backend pinned | `control:pinned` `file:actions/setup/action.yml` `file:uv.lock` | ✅ |
| 21 | Automated dependency updates that wait out a cooldown. | [OpenSSF: Dependency-Update-Tool](https://github.com/ossf/scorecard/blob/main/docs/checks.md#dependency-update-tool) | `deps-bot` and `cooldown` controls; `exclude-newer` in this repository | `control:deps-bot` `control:cooldown` `scenario:scorecard/cooldown-in-a-comment` | ✅ |
| 22 | Least-privilege workflow tokens. | [OpenSSF: Token-Permissions](https://github.com/ossf/scorecard/blob/main/docs/checks.md#token-permissions) | `permissions: {}` defaults; top-level write scopes fail the scorecard | `control:permissions` `scenario:scorecard/write-all-is-not-least-privilege` `scenario:scorecard/top-level-write-scope` | ✅ |
| 23 | No dangerous workflow patterns; audit the workflows themselves. | [OpenSSF: Dangerous-Workflow](https://github.com/ossf/scorecard/blob/main/docs/checks.md#dangerous-workflow) | No `pull_request_target`; zizmor and actionlint in `security.yml` | `job:security.yml#workflows` `scenario:scorecard/pull-request-target-is-not-pr-ci` | ✅ |
| 24 | Secrets are never committed. | [Microsoft SDL](https://learn.microsoft.com/en-us/compliance/assurance/assurance-microsoft-security-development-lifecycle) | gitleaks from a digest-pinned image on every PR and push | `job:security.yml#secrets` | ✅ |
| 25 | Known vulnerabilities: a PR is blocked on what it introduces; the main line on everything. | Google: [OSV-Scanner PR workflow](https://google.github.io/osv-scanner/github-action/); [OpenSSF: Vulnerabilities](https://github.com/ossf/scorecard/blob/main/docs/checks.md#vulnerabilities) | Base-vs-head OSV diff on PRs; blocking on push and schedule; dependency review | `job:security.yml#dependencies` | ✅ |
| 26 | Signed build provenance and an SBOM for every release artifact. | [SLSA v1.2 Build L2/L3](https://slsa.dev/spec/v1.2/build-requirements); [GitHub attestations](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-and-reusable-workflows-to-achieve-slsa-v1-build-level-3) | Container images: Sigstore-signed SLSA provenance and an SPDX SBOM from `container.yml`, a reusable workflow. GitHub documents this pattern as reaching Build L3 *when the verifier pins the signer workflow*, as row 27 does. Release wheels: `release.yml` is a plain tag workflow, so Build L2, with the Sigstore bundle attached to the release for offline verification | `job:container.yml#publish` `job:release.yml#release` `control:provenance` `file:.github/workflows/release.yml` | ✅ |
| 27 | Consumers verify provenance before deploying. | [SLSA v1.2 Build](https://slsa.dev/spec/v1.2/build-requirements) ("consumers MUST be able to validate") | `actions/verify-image` checks provenance and the SBOM, signer workflow and repository, by digest | `file:actions/verify-image/action.yml` `file:templates/deploy-verify.yml` | ✅ |
| 28 | Track repository security posture continuously. | [OpenSSF Scorecard](https://github.com/ossf/scorecard) | The golden scorecard (L1–L4) gates every PR; OpenSSF Scorecard runs alongside | `cmd:scorecard` `control:ossf` `job:scorecard.yml#analysis` | ✅ |

## E. Measurement and continuous improvement

| # | Practice | Source | Control here | Evidence | Status |
|---|---|---|---|---|---|
| 29 | Measure delivery with the DORA metrics, including rework rate (2024). | [DORA metrics](https://dora.dev/guides/dora-metrics/) | `golden dora` computes all five from a deployment log | `cmd:dora` `scenario:dora/basic` `scenario:dora/rework-rate` | ✅ |
| 30 | Measure the gates themselves: false positives, escapes, productive findings. | [Google TSE 2021](https://arxiv.org/abs/2102.11378) (productive mutants); [SWE at Google ch. 20](https://abseil.io/resources/swe-book/html/ch20.html) (under 10% effective false positives) | A decision benchmark of labelled PRs scored as escapes and false blocks on every change; strict xfail for known gaps | `file:evals/runner.py` `test:tests/test_evals.py::test_corpus_is_labelled` `scenario:presence/django-tests-py` | ✅ |

## F. Deliberately not adopted

| # | Practice | Source | Decision | Evidence | Status |
|---|---|---|---|---|---|
| 31 | A speculative merge engine with a conflict analyser. | [Uber SubmitQueue](https://www.masoud.io/docs/eurosys19.pdf) | GitHub's merge queue covers the scale of the teams this pipeline serves. | Not adopted | ⛔ |
| 32 | Change-based or predictive test selection. | [Meta, ICSE-SEIP 2019](https://arxiv.org/abs/1810.05286) | Diff coverage, the ratchet and mutation all need the full suite's coverage report; a subset would turn covered lines into false "uncovered" blocks. | Not adopted | ⛔ |
| 33 | Gating merges on the aggregate OpenSSF score. | [OpenSSF checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md) | Several checks (Contributors, Maintained, Fuzzing) measure project health, not change safety. The score runs alongside but never blocks. | Not adopted | ⛔ |
| 34 | Two approving reviewers on every change (Branch-Protection, tier 4). | [OpenSSF checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md#branch-protection) | SLSA Source L4 counts the author and one reviewer as the "two trusted persons", and Google requires one approval plus an owner's. A second reviewer adds review latency, which DORA's [change-approval research](https://dora.dev/capabilities/streamlining-change-approval/) counts against delivery. The `reviews` control requires at least one; a team can raise the ruleset's count. | Not adopted | ⛔ |
| 35 | Source provenance and source verification summary attestations, which every SLSA Source level requires. | [SLSA v1.2 source requirements](https://slsa.dev/spec/v1.2/source-requirements) | No first-party GitHub feature issues them yet, and [slsa-framework/source-tool](https://github.com/slsa-framework/source-tool) is in development. The review and history controls match Source L2 to L4, but no formal Source level is claimed. | Not adopted | ⛔ |

## G. Standards crosswalk

The same controls, by the identifiers each standard uses. Every identifier is checked by
`tests/test_alignment.py` against the standard's published list, so a mistyped or withdrawn ID
fails the suite: SSDF v1.1 renumbered several tasks, and PW.3.1, PW.3.2, PW.4.3, PW.4.5 and
PW.5.2 are now only "moved to" pointers.

Sources, all opened on 2026-10-22:

- [NIST SP 800-218 SSDF v1.1](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-218.pdf).
  This is the final version; Rev. 1 (v1.2) is an initial public draft.
- [SLSA v1.2](https://slsa.dev/spec/v1.2/).
- [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md).
- [OWASP SAMM v2](https://owaspsamm.org/model/).
- [DORA capabilities](https://dora.dev/capabilities/).

SSDF covers security vulnerabilities and never names coverage, mutation or flaky tests. Mappings
marked *interpretive* rest on the general wording of a task, not on a direct statement.

| Control area | Matrix rows | NIST SSDF v1.1 | SLSA v1.2 | OpenSSF Scorecard | OWASP SAMM v2 | DORA capability |
|---|---|---|---|---|---|---|
| Testing gates: diff coverage, ratchet, mutation, presence, flakes | `rows:1-8` | `ssdf:PW.8.1` `ssdf:PW.8.2` `ssdf:PO.4.1` `ssdf:PO.4.2` (interpretive) | | `openssf:CI-Tests` | `samm:V-RT-A-3` `samm:I-SB-A-3` | `dora:test-automation` `dora:continuous-integration` |
| Findings on the diff, fail closed | `rows:9-10` | `ssdf:PW.7.1` `ssdf:PW.7.2` `ssdf:PW.5.1` | | `openssf:SAST` | `samm:V-ST-A-3` | `dora:code-maintainability` |
| Policy as code, floors, expiring exceptions | `rows:11-12` | `ssdf:PO.1.2` (Ex 7, exception handling) `ssdf:PO.4.1` (Ex 5) `ssdf:PW.1.2` (Ex 3) | | | `samm:G-PC-A-2` | |
| Separation of duties: base-anchored policy, owner review, two-person review | `rows:13-15` | `ssdf:PS.1.1` (Ex 4, "Have the code owner review and approve") `ssdf:PO.2.1` `ssdf:PW.7.2` | `slsa:source-L3` `slsa:source-L4` (requirements matched, no level claimed) | `openssf:Branch-Protection` `openssf:Code-Review` | | `dora:streamlining-change-approval` |
| Mainline green, presubmit | `rows:16-17` | `ssdf:PO.4.2` (Ex 3, automated decisions) | | `openssf:Branch-Protection` | `samm:I-SB-A-2` | `dora:trunk-based-development` `dora:continuous-integration` |
| Test what ships, release from main | `rows:18-19` | `ssdf:PW.6.2` `ssdf:PO.5.1` | `slsa:build-L2` | | `samm:I-SD-A-2` | `dora:deployment-automation` `dora:continuous-delivery` |
| Pinned and updated dependencies | `rows:20-21` | `ssdf:PW.4.1` `ssdf:PW.4.4` `ssdf:PO.3.2` | | `openssf:Pinned-Dependencies` `openssf:Dependency-Update-Tool` | `samm:I-SB-B-2` | |
| Least privilege, workflow audit | `rows:22-23` | `ssdf:PS.1.1` `ssdf:PO.5.1` `ssdf:PW.7.2` | | `openssf:Token-Permissions` `openssf:Dangerous-Workflow` | | `dora:pervasive-security` |
| Secret scanning | `rows:24-24` | `ssdf:PW.7.2` (Ex 6, interpretive) | | | | `dora:pervasive-security` |
| Known vulnerabilities, disclosure policy | `rows:25-25` | `ssdf:PW.4.4` (Ex 1, 2) `ssdf:RV.1.1` `ssdf:RV.1.2` `ssdf:RV.1.3` `ssdf:RV.2.2` | | `openssf:Vulnerabilities` `openssf:Security-Policy` | `samm:I-SB-B-3` | `dora:pervasive-security` |
| Provenance and SBOM | `rows:26-26` | `ssdf:PS.2.1` `ssdf:PS.3.1` `ssdf:PS.3.2` | `slsa:build-L2` `slsa:build-L3` (container images, verifier-pinned) | `openssf:Signed-Releases` `openssf:SBOM` | `samm:I-SB-B-1` | |
| Verify before deploy | `rows:27-27` | `ssdf:PW.4.4` (Ex 6) `ssdf:PO.1.3` | `slsa:build-L2` (consumer: "Validate the authenticity of the provenance") | | `samm:I-SD-A-3` | `dora:deployment-automation` |
| Delivery metrics, measured gates, escaped-defect review | `rows:29-30` | `ssdf:RV.3.1` `ssdf:RV.3.2` `ssdf:RV.3.3` `ssdf:RV.3.4` `ssdf:PO.4.1` (Ex 6) | | | `samm:I-DM-B-1` `samm:I-DM-B-2` `samm:I-DM-B-3` | `dora:continuous-delivery` |

### Where a standard asks for more than this pipeline gives

- **Bypass actors.** OpenSSF treats any ruleset bypass actor as "admins not enforced". A
  `contents: read` token cannot see bypass actors, so the scorecard cannot check them. Audit them
  in the organisation settings.
- **Hardened egress.** SSDF PO.5.1 Ex 3 asks to restrict build-environment connections to "only
  what is necessary". `golden.yml` audits egress by default; `egress-policy: block` with an
  allowlist is the caller's choice, because the endpoints depend on the project's toolchain.
- **Secret scanning and dependency cooldowns** have no direct SSDF v1.1 wording. The mappings
  above are interpretive.
