# Gate decision benchmark

`scripts/drill.py` asks whether each gate catches a weak test *with real toolchains*. This
benchmark asks a broader question: **does every gate make the right merge decision across the
pull requests it will really see?** That includes legitimate layouts it must not block, attempts
to get around it, and broken inputs it must not crash on or wave through.

```console
$ uv run python evals/runner.py                 # all scenarios, markdown report
$ uv run python evals/runner.py policy/ flake/  # scenario-id prefixes
$ uv run python evals/runner.py --json out.json # machine-readable results
$ uv run pytest tests/test_evals.py             # the same, as part of the test suite
```

## How a scenario runs

Each `[[scenario]]` in `scenarios/*.toml` describes:

- a base commit (`[scenario.base]` files on `main`)
- a pull request (`[scenario.head]` files, `delete = [...]`)
- CI artifacts that exist in the workspace but not in the diff (`[scenario.artifacts]`: coverage and
  mutation reports, baselines)
- the `golden` command to run (`argv`), with placeholders `{base}`, `{root}`, `{tmp}`, `{in30days}`
  and `{in60days}`
- what should happen (`expect`, `expect_lines`, `expect_json`, `expect_outputs` or
  `stdout_contains`)

The runner builds the repository with git in a temporary directory and simulates the
`pull_request` event (`GITHUB_EVENT_PATH`, with `labels`). It then runs `python -m golden` from
this checkout. It removes ambient CI variables first, so a scenario behaves the same on a laptop,
in CI and under mutmut.

Scenarios with `flake = N` run the flake gate for real. `golden changed --kind tests` selects
the files, and pytest re-runs them N times. As in `golden.yml`, the step is skipped when nothing
is selected.

## Ground truth and scoring

Every scenario has a `category`, which is its ground-truth label:

| Category | Meaning | A wrong result counts as |
|---|---|---|
| `legit` | a reasonable change the gate must let through or handle correctly | **false block** |
| `gaming` | a change that dodges the bar (no tests, weak tests, self-granted waivers, commented-out controls) | **escape** |
| `robustness` | real but unusual input: corrupt reports, odd file names, malformed config | **robustness failure** |

Outcomes are classified the way the merge sees them:

- `pass`: exit 0
- `block`: exit 1
- `error`: exit 2, a fail-closed refusal with a message
- `crash`: a traceback, which is never acceptable

A scenario can carry `known_gap = "why"`. That documents a limitation on purpose. The runner
reports it but doesn't fail on it, and pytest treats it as a strict xfail. Once someone fixes the
gap, the suite fails until the label is removed, so the gap list stays honest.

## Results

The same scenarios, run against the code before and after the hardening in this repository:

| | Scenarios | Correct | Escapes | False blocks | Robustness failures | Crashes | Known gaps |
|---|---|---|---|---|---|---|---|
| v1.0.0 (initial release) | 95 | 31 | **30** | **20** | **13** | 7 | 1 |
| now | 95 | 94 | 0 | 0 | 0 | 0 | 1 |

Crashes are robustness failures too, so they are counted in both columns.

18 of v1.0.0's 63 wrong decisions come from controls it doesn't have, such as test-weakening
scope, the self-gating warning, owned OSV exceptions, the arid-line filter and Python survivors shown on the line. The other 45 are defects in gates it
already shipped. [Value](../docs/value.md#1-the-right-merge-decision) breaks them down.

The worst escapes before the hardening, all now closed:

- A pull request could lower its own thresholds, add its own waiver or exemption, or delete
  `golden.toml`. The gates read the policy from the branch under review.
- A typo in `golden.toml` (`diff_minimum`), `nan`, or a fractional repeat count was accepted
  silently.
- A test that only *mentioned* the module in a comment satisfied test presence.
- Scorecard controls were met by comments: a `# TODO: container.yml`, a commented-out
  `cooldown:`, a `pull_request_target` trigger, `permissions: write-all`, or a lockfile under
  `node_modules/`.
- A mutation report with absolute paths was filtered down to "no mutants" and passed.

The most common false blocks before the hardening, all now fixed:

- Django's `app/tests.py`.
- Relative imports (`from ..core import f`).
- Helpers under `pkg/tests/`.
- Angular/Nest file names (`order.service.ts`) and explicit `./cart/index.js` imports.
- A change to only `conftest.py`. The flake gate ran pytest on it and pytest exited 5.
- Multi-line `test-command` inputs.

One known gap remains, on purpose and in the open:

- `presence/python-string-mention`: a module name inside an unrelated string literal still counts
  as a reference, because strings are how `mock.patch("pkg.mod.f")` names its target. Mutation
  testing is the backstop.

A second gap is now closed. `mutation-targets/test-only-weakening` is a pull request that only
deletes assertions from an existing test, so it changes no source line. The functions a changed
test names now join the mutation scope ([red-team review](../docs/redteam/2026-10-22-review.md),
R5), so the scenario passes, and so do four more like it.

## Adding a scenario

1. Write the change as a reviewer would describe it in `why`, and label it honestly.
2. Run `uv run python evals/runner.py <id>` against the *old* behaviour first. A new scenario
   that passes before the fix proves nothing.
3. Fix the gate, then add the unit tests that pin the fix in `tests/`.

Good sources of new scenarios:

- every escaped defect in the post-incident review
  ([evaluation](../docs/evaluation.md#22-escaped-defect-review))
- every gate failure an author disputes and a reviewer upholds
- layouts from real repositories that adopt the pipeline
