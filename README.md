# Golden Pipeline

**Reusable GitHub Actions pipelines that let a team ship without a manual QA gate.**
Every job a QA engineer used to do by hand is a merge gate here: it can't be skipped, it's measured,
and its own code is tested to the same bar it enforces.

```yaml
# .github/workflows/ci.yml in your repository
name: ci
on:
  pull_request:
    # labeled/unlabeled: the test-presence opt-out label takes effect at once.
    types: [opened, synchronize, reopened, labeled, unlabeled]
  push:
    branches: [main]
  merge_group:
permissions: {}
jobs:
  ci:
    uses: Mira190/golden-pipeline-open/.github/workflows/golden.yml@<commit-sha> # v1.0.0
    permissions:
      contents: read
    with:
      language: python   # or node, go
```

Then require the single status check **`ci / golden`** in a branch ruleset. That's the whole integration.

## What runs on every pull request

| Stage | Python | TypeScript / Node | Go |
|---|---|---|---|
| Format and lint | ruff | Biome | gofmt, go vet, golangci-lint v2 |
| Types | mypy (strict if you configure it) | `tsc --noEmit` | `go build` |
| Tests | pytest in random order across a version matrix | Vitest with shuffled order | `go test -race -shuffle=on` |
| **Diff coverage** | changed lines ≥ 90% covered | same | same |
| **Test presence** | every changed source file ships with a changed test that references it | same | same |
| **Coverage ratchet** | total coverage never drops more than 0.5 pp below the default branch | same | same |
| **Flake gate** | changed tests re-run 10× and must pass every time | same | same |
| **Mutation testing** | mutmut, scoped to changed functions | StrykerJS, scoped to changed lines | gremlins, scoped to changed lines |
| Supply chain | gitleaks, OSV-Scanner, dependency review, zizmor, actionlint | same | same, plus govulncheck |
| Repository scorecard | maturity level L1–L4 (rulesets, pinning, owners, cooldowns) | same | same |
| Container (optional) | build → smoke test → SBOM → grype → signed SLSA provenance | same | same |

Bold gates are the ones that replace "did they test it properly?":

- **Diff coverage** checks the new code is executed.
- **Test presence** checks someone wrote a test for it.
- **Mutation testing** checks the tests would actually notice a bug.
- **The flake gate** checks those tests are trustworthy.

Coverage alone can be gamed (see the [drills](docs/evaluation.md#3-drills-does-each-gate-catch-what-it-claims)). The four together are hard to game.

## Why trust it

- **Evidence-based.** The design follows published practice from Google, Meta, Amazon, Microsoft, Uber and Shopify, and the academic literature on coverage, mutation testing and flakiness ([research](docs/research.md)).
- **Dogfooded.** This repository is gated by its own workflows. The `golden` CLI and three example projects ([Python](examples/python), [TypeScript](examples/node), [Go](examples/go)) all run through `golden.yml` on every change. The CLI has 99% branch coverage and passes its own 80% mutation bar.
- **Measured, not asserted.** A decision benchmark of 80 labelled pull requests ([evals](evals/README.md)) replays legitimate changes, attempts to game each gate, and malformed inputs through the real CLI on every change: 0 escapes, 0 false blocks, 0 crashes, one documented gap. Against v1.0.0 it finds 21 escapes and 16 false blocks, all now fixed.
- **A change never grades itself.** Gates read `golden.toml` from the pull request's base commit, so a PR can tighten the bar but not lower it, waive it or exempt itself. The scorecard requires code-owner review of workflows and policy.
- **Fails closed.** A missing or empty report, a source file the coverage report never measured, a mutation run where most mutants time out or none get a verdict, or a weakened threshold fails the gate instead of passing quietly.
- **Shift left.** `golden local --language python` runs the same commands and the same gate code on a laptop or inside a coding agent's loop, before the push. It is also available as pre-commit hooks pinned to the same SHA ([adoption](docs/adoption.md#5-shift-left-the-same-verdict-before-you-push)).
- **Hardened for 2025–2026 supply-chain attacks:**
  - every action pinned to a commit SHA
  - `permissions: {}` by default
  - no `pull_request_target`
  - release jobs never restore caches
  - gitleaks runs from a digest-pinned image
  - dependency installs skip lifecycle scripts
  - every ecosystem gets a dependency cooldown

  See [security model](docs/security-model.md).
- **Versioned as one unit.** The reusable workflow installs the `golden` CLI from the *same commit* you pinned, via `$/actions/setup`. A pinned SHA therefore freezes both the YAML and the gate logic.

## Policy as code

Thresholds live in an optional `golden.toml` next to your project. A repository may make any gate stricter. It can never make a gate weaker than the platform floor. The only way below the floor is a waiver, which needs a reason and expires within 90 days:

```toml
[coverage]
diff_min = 95            # floor 80
[mutation]
min_score = 85           # floor 60
[waivers.mutation]
reason = "legacy billing module, PLAT-123"
expires = 2026-12-31     # gate is report-only until then, then enforced again
```

See [configuration](docs/configuration.md) for every key, every workflow input and the per-language default commands.

## Documentation

| | |
|---|---|
| [Research](docs/research.md) | How large engineering organisations ship without manual QA, with citations |
| [Architecture](docs/architecture.md) | Job graph, versioning, data flow and design decisions |
| [Gates](docs/gates.md) | What each gate checks, how it's computed and how to fix a failure |
| [Configuration](docs/configuration.md) | `golden.toml`, workflow inputs, language defaults |
| [Evaluation](docs/evaluation.md) | How the gates themselves are tested, maturity model, effectiveness metrics, drills, DORA |
| [Decision benchmark](evals/README.md) | 80 labelled pull requests every gate must decide correctly |
| [Adoption](docs/adoption.md) | Rolling it out to existing repositories, rulesets, monorepos |
| [Security model](docs/security-model.md) | Threats, the incidents behind each control, and the residual risk |

## Repository layout

```
.github/workflows/golden.yml      the reusable pipeline (python | node | go)
.github/workflows/security.yml    secrets, dependencies, workflow audit (also usable standalone)
.github/workflows/container.yml   build, scan, SBOM, signed provenance
actions/setup/                    installs uv + the golden CLI at the caller's pinned commit
src/golden/                       the gate logic (Python, typed, mutation-tested)
evals/                            decision benchmark: labelled PR scenarios through the real gates
examples/{python,node,go}/        small services gated by this pipeline in CI
templates/                        copy-paste starting points for consumers
docs/                             everything above
```

## License

[Apache-2.0](LICENSE)
