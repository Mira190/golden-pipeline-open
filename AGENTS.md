# Instructions for coding agents

This repository is gated by its own Golden Pipeline. CI blocks a merge on every check below, so
run them yourself before you push. A red CI run costs a review cycle; a local run costs seconds.

## Before every push

```bash
scripts/local.sh          # the exact checks and gates of ci.yml's golden-cli job
scripts/local.sh --fast   # policy, format, lint, types only (while iterating)
```

Commit first: the diff gates (diff coverage, test presence, flake re-runs) only see committed
work, as in CI. `golden local` warns when the working tree is dirty.

## What the gates expect from a change

- **Every changed source file in `src/golden/` ships with a changed test** in `tests/` that
  references the module (test presence).
- **≥ 95% of changed lines covered** (diff coverage, `golden.toml`), with branch coverage on.
- **Mutation score ≥ 80% on the functions you change.** Assert on values and boundaries, not
  just "does not raise". To check one function:
  `uv run mutmut run "golden.config.x_from_mapping__mutmut_*"`.
- **Changed tests pass 10 times in random order** (`pytest-randomly`). Don't depend on test
  order, wall-clock time or the network. Tests run under a `hermetic_env` fixture that clears the
  `GITHUB_*` context.
- `ruff check`, `ruff format --check` and `mypy --strict` are clean.

## Rules of this codebase

- **Gates fail closed.** A missing, empty or unparsable input (report, policy, git output) is an
  error, never a pass. Add a test for the failure path whenever you add an input.
- Never weaken `golden.toml`, a floor in `src/golden/config.py`, or a workflow's `permissions`
  to get a change through. CODEOWNERS review those paths.
- Pin every new action to a full commit SHA with a `# vX.Y.Z` comment. Pin every tool version to
  a release at least 7 days old.
- Workflow `run:` blocks read inputs through `env:`, never `${{ }}` inside the script.
- When you change a default command in `src/golden/plan.py`, run
  `uv run python scripts/drill.py <language>` and update `docs/configuration.md`.
