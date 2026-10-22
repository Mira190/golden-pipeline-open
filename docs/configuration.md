# Configuration

There are two layers:

- **Workflow inputs** decide *how* each step runs: commands, versions, directories.
- **`golden.toml`** decides *how strict* the gates are. It lives in the project directory
  (`working-directory`) and is optional.

## golden.toml

| Key | Default | Floor | Strictest | Meaning |
|---|---|---|---|---|
| `coverage.diff_min` | 90 | ≥ 80 | ≤ 100 | minimum % of changed coverable lines covered |
| `coverage.ratchet_tolerance` | 0.5 | ≤ 2 | ≥ 0 | allowed drop in total coverage (percentage points) vs the default branch |
| `mutation.min_score` | 80 | ≥ 60 | ≤ 100 | minimum mutation score on changed code |
| `flaky.repeats` | 10 | ≥ 3 | ≤ 100 | times each changed test is re-run (whole number) |
| `scorecard.min_level` | 2 | ≥ 1 | ≤ 4 | minimum repository maturity level (whole number) |
| `presence.exempt` | `[]` | – | – | extra globs excluded from the test-presence gate and mutation |

The policy is validated in the first job (`golden policy check`), and the effective values are
printed to the job summary. Validation fails closed, because a mistake that silently fell back to
a default would weaken a gate without anyone noticing:

- Unknown sections **and unknown keys** fail validation (`diff_minimum = 95` is an error, not
  "90").
- Non-numeric, `nan` and `inf` values fail validation, and so do fractional `repeats` or levels.
- Any value weaker than its floor fails with an explanation.
- Any value beyond "strictest" fails too: `min_level = 5` or `diff_min = 101` could never pass,
  so every change would be blocked forever.

```toml
[coverage]
diff_min = 95
ratchet_tolerance = 0.2

[mutation]
min_score = 85

[presence]
exempt = ["src/generated/**", "**/legacy_adapter.py"]
```

Protect the file with CODEOWNERS, so changes to the bar get the same review as code.

### A change never grades itself

On a pull request (and in the merge queue) every gate reads `golden.toml` as it is on the
**base commit**, then lets the change tighten it but never loosen it:

| The pull request… | Applies to this pull request? |
|---|---|
| raises a threshold, shortens a waiver, removes a waiver or an exemption | yes, immediately |
| lowers a threshold (still above the floor) | no: the merged value applies until it is merged |
| adds or extends a waiver | no: the merged waiver (or none) applies |
| adds a `presence.exempt` glob | no |
| deletes `golden.toml` | no: the merged values still apply |

What was deferred is listed under **Not applied to this change** in the policy summary and as a
notice annotation. The base comes from `--base` or, in Actions, from the triggering event; on a
push to the default branch there is no base and the file is read as is. If the merged file is
itself invalid under the current rules (for example after a platform floor was raised), the
pull request that repairs it is judged by its own policy.

### Waivers

A waiver turns one gate into report-only for a limited time:

```toml
[waivers.mutation]
reason = "inherited module without tests; tracked in PLAT-123"
expires = 2026-12-31
```

- Allowed gates: `diff-coverage`, `coverage-ratchet`, `test-presence`, `mutation`, `scorecard`.
- `reason` is required.
- `expires` is a TOML date at most **90 days** ahead.
- After expiry the gate is enforced again automatically.
- While any waiver is active, the scorecard cannot reach L4.

## Workflow inputs (`golden.yml`)

| Input | Default | Notes |
|---|---|---|
| `language` | *required* | `python`, `node` or `go` |
| `working-directory` | `.` | project directory; diffs, reports and annotations are scoped to it |
| `versions` | per language | JSON list for the test matrix; the **last** entry runs lint, gates and mutation |
| `install-command` | per language | empty = default; Node picks npm/pnpm/yarn/bun from the lockfile |
| `format-command` | per language | empty string = default; Node's default is empty because `biome ci` formats too |
| `lint-command` | per language | Go additionally runs golangci-lint v2 unless you override this |
| `typecheck-command` | per language | |
| `test-command` | per language | must write `coverage-report` (Cobertura XML or LCOV) |
| `coverage-report` | per language | path relative to `working-directory` |
| `flaky-command` | per language | receives `$GOLDEN_TESTS`, `$GOLDEN_TEST_PACKAGES`, `$GOLDEN_REPEATS` |
| `mutation-command` | per language | receives `$GOLDEN_MUTATE`, `$GOLDEN_BASE` |
| `mutation-report` / `mutation-format` | per language | format: `mutmut`, `stryker`, `mutation-testing-elements` (alias of `stryker`, for Stryker.NET/Stryker4s), `gremlins` |
| `mutation` | `true` | set `false` to skip mutation testing; this fails the scorecard's L3 "no gate switched off" control, so prefer a waiver |
| `security` | `true` | run the nested security workflow; `false` fails the same L3 control |
| `skip-presence-label` | `no-tests-needed` | PR label that makes test presence report-only |
| `runs-on` | `ubuntu-24.04` | runner label |
| `egress-policy` | `audit` | harden-runner policy; `block` needs an allowlist |

## Language defaults

The source of truth is [`src/golden/plan.py`](../src/golden/plan.py). Every tool version is pinned
and was at least 7 days old when pinned.

### Python (uv)

| Step | Default |
|---|---|
| versions | `["3.12", "3.13"]` |
| install | `uv sync --locked` |
| format | `uvx ruff@0.17.0 format --check --diff .` |
| lint | `uvx ruff@0.17.0 check --output-format=github .` |
| typecheck | `uv run --with mypy==2.4.0 mypy .` |
| test | `uv run --with pytest-cov==7.1.0 --with pytest-xdist==3.8.0 --with pytest-randomly==5.0.0 pytest -n auto -p randomly --cov --cov-report=xml:coverage.xml` |
| mutation | `mutmut run $GOLDEN_MUTATE && mutmut export-cicd-stats` (mutmut 3.8.0) |

Requirements:

- a `uv.lock`
- pytest in your dev dependencies (the plugins are added with `--with`)
- `[tool.coverage.run] relative_files = true`
- a `[tool.mutmut]` section (`source_paths = ["src/yourpkg/"]`)

### TypeScript / Node

| Step | Default |
|---|---|
| versions | `["22", "24"]` |
| install | `npm ci --ignore-scripts` (or `pnpm install --frozen-lockfile --ignore-scripts`, `yarn install --immutable --mode=skip-build`, `bun install --frozen-lockfile --ignore-scripts`) |
| lint (+format) | `npx --no-install biome ci .` |
| typecheck | `npx --no-install tsc --noEmit` |
| test | `npx --no-install vitest run --coverage.enabled --coverage.reporter=cobertura --coverage.reporter=text --sequence.shuffle` |
| mutation | `npx --no-install stryker run --mutate "$GOLDEN_MUTATE" --reporters json,clear-text` |

Requirements:

- devDependencies: `@biomejs/biome`, `typescript`, `vitest`, `@vitest/coverage-v8`,
  `@stryker-mutator/core`, `@stryker-mutator/vitest-runner`
- a Stryker config with `"testRunner": "vitest"`

Compatibility notes (October 2026):

- StrykerJS 10 calls the TypeScript compiler API, which TypeScript 7 (the native port) no longer
  has. Use TypeScript 6.0.x with Stryker.
- Stryker's Vitest runner works with Vitest 4.x. With Vitest 5 it ran 0 tests per mutant in our
  drill.
- The example project pins TypeScript 6.0.3 and Vitest 4.1.11.

### Go

| Step | Default |
|---|---|
| versions | `["go.mod"]` (setup-go reads `go-version-file`) |
| format | `test -z "$(gofmt -l .)"` |
| lint | `go vet ./... && govulncheck ./...` (v1.9.0), plus golangci-lint v2.14.0 via its action |
| typecheck | `go build ./...` |
| test | `go test -race -shuffle=on -covermode=atomic -coverprofile=coverage.out ./...`, then gocover-cobertura v1.5.0 |
| mutation | `gremlins unleash --timeout-coefficient 10 --output gremlins.json .` (v0.6.0) |

Requirements: a `go.mod`. A `.golangci.yml` (config `version: "2"`) is recommended.

## Other reusable workflows

### security.yml

Inputs: `working-directory`, `fail-on-severity` (`high`), `allow-licenses` (empty = no licence
gate), `runs-on`, `egress-policy`. It needs only `contents: read`.

### container.yml

Inputs:

- `context`
- `dockerfile`
- `image`: defaults to the repository name
- `smoke-command`: gets `$IMAGE`
- `severity-cutoff`: default `high`
- `runs-on`
- `egress-policy`

Output: `digest`. The caller must grant `contents: read`, `packages: write`, `id-token: write` and
`attestations: write`; publishing happens only on pushes to the default branch.
