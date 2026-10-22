# Contributing

This repository is gated by its own pipeline, so a contribution goes through the same gates it
ships: diff coverage ≥ 95%, test presence, flake re-runs and mutation score ≥ 80% on the lines you
change.

```bash
uv sync
scripts/local.sh                   # everything ci.yml's golden-cli job checks, before you push
scripts/local.sh --fast            # policy, format, lint, types only
uv run pytest -n auto              # unit + end-to-end tests of the gates
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run mutmut run "golden.cli.x_cmd_presence__mutmut_*"   # mutate what you changed
uv run python scripts/drill.py python                     # gate drills (needs node and go for all)
```

Changing a default command or tool version in `src/golden/plan.py`:

- pin an exact version that was released at least 7 days ago
- run the drills for that language
- update `docs/configuration.md`

Releases: bump `version` in `pyproject.toml` and `src/golden/__init__.py`, merge, then tag
`vX.Y.Z` on `main`. The release workflow checks the versions match.
