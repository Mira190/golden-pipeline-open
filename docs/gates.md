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

**Fixing a failure:** write a test that exercises the annotated lines. If a line is genuinely
untestable (defensive `raise`, platform branch), exclude it the way your coverage tool supports
(`# pragma: no cover`, `/* v8 ignore next */`). Reviewers see the exclusion in the diff.

## Test presence

**Question:** did someone write a test for this change?

Every changed source file needs at least one *changed* test file in the same PR that references it:

| Language | A test references `src/x.ext` when… |
|---|---|
| Python | the test imports or mentions the dotted module (`pkg.x`), or the module name as a word |
| Node | the test is named `x.test.*` / `x.spec.*`, or imports a path ending in `/x` (with or without extension) |
| Go | the test file lives in the same package directory |

Ignored automatically: generated code (`*.pb.go`, `*_gen.go`, `*.d.ts`), config files, `vendor/`,
`node_modules/`, migrations, tool work directories (`mutants/`, `.stryker-tmp/`). Add more with
`presence.exempt` globs.

**Escape hatch:** the PR label `no-tests-needed` (configurable with `skip-presence-label`) makes the
gate report-only for that PR. This is meant for pure refactors covered by existing tests. The label
and the report-only verdict are visible to reviewers, and diff coverage still applies.

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
single failure blocks the merge. Most flaky tests are flaky from the moment they're written
(Luo et al., FSE 2014), so this is the cheapest point to catch them.

| Language | Default re-run |
|---|---|
| Python | `pytest -n auto -p randomly --count=N <files>` (pytest-repeat) |
| Node | N fresh `vitest run --sequence.shuffle <files>` processes |
| Go | `go test -race -shuffle=on -count=N <packages>` |

## Mutation testing

**Question:** would these tests notice a bug?

The mutation tool injects small faults (`>` → `>=`, `+` → `-`, `return x` → `return None`)
**into the changed code only** and runs the tests against each one. The gate passes when the
score reaches `mutation.min_score` (default 80%, floor 60%):

```
score = (killed + timed out) / (killed + timed out + survived + not covered)
```

Compile errors, runtime errors, ignored and skipped mutants are excluded. The definition is
Stryker's, applied to all three tools so the number means the same thing everywhere.

| Language | Tool | How it is scoped |
|---|---|---|
| Python | mutmut 3.8 | changed functions and methods → `mutmut run "pkg.mod.x_func__mutmut_*"` |
| Node | StrykerJS 10 | changed line ranges → `stryker run --mutate "src/a.ts:10-25,…"` |
| Go | gremlins 0.6 | whole module, then `golden mutation --base` keeps mutants on changed lines |

**Fails closed:**

- A missing report is an error ("did the mutation tool run?").
- A run where more than half of the mutants time out is marked unreliable and fails. The fix is
  to raise the tool's timeout.

**Fixing a failure:** each surviving mutant is annotated on its line with the mutator name. Write
the assertion that would have failed. For example, a surviving `CONDITIONALS_BOUNDARY` on
`if qty >= 50` means no test checks quantity 49 *and* 50.

**Arid code:** lines where no reasonable test should care (help text, log messages) can be
excluded. Use `# pragma: no mutate` or mutmut's `do_not_mutate_patterns`, `// Stryker disable
next-line`, or `--exclude-files` for gremlins. Keep the list short and commented. Google found
that filtering arid nodes is what turns mutation testing from noise (15% of mutants useful) into
signal (89%).

## Repository scorecard

**Question:** is the repository set up so that the gates can't be bypassed?

`golden scorecard` checks 15 controls in four cumulative levels and fails below
`scorecard.min_level` (default 2):

| Level | Controls |
|---|---|
| **L1 Adopted** | CI on pull requests · calls `golden.yml` · dependencies locked |
| **L2 Owned** | CODEOWNERS · Dependabot/Renovate · SECURITY.md |
| **L3 Enforced** | ruleset requires the `golden` check · ruleset requires an approval · force-push and deletion blocked · every action pinned to a SHA · every workflow declares `permissions:` · dependency cooldown configured |
| **L4 Verified** | release artifacts carry signed provenance · OpenSSF Scorecard runs · no active waivers |

The ruleset controls are read from the GitHub API (`GET /repos/{repo}/rules/branches/{branch}`,
which works with `contents: read`). When the API isn't reachable they show **unknown**, and
unknown never counts as a pass.

## Security (nested workflow)

| Check | Tool | Blocks on |
|---|---|---|
| Committed secrets | gitleaks 8.30.1 (digest-pinned image); PR range on PRs, full history on pushes | any finding |
| Vulnerable dependencies | OSV-Scanner 2.7 over every lockfile in the project directory | any known vulnerability |
| New dependencies in the PR | dependency-review-action | severity ≥ `high` (configurable); licences outside `allow-licenses` if set |
| Workflow security | zizmor 1.30 (`regular` persona) | any finding (template injection, excessive permissions, unpinned actions, cache poisoning, …) |
| Go reachability | govulncheck 1.9 (in the Go lint step) | a reachable vulnerable symbol |

## Verdict

The `golden` job aggregates everything above. Make it the **only** required status check:

- Adding or removing gates never requires editing rulesets.
- A cancelled or failed job can never be silently ignored.
