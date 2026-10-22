#!/usr/bin/env bash
# Shift left: everything ci.yml's `golden-cli` job checks, with the same input
# overrides, before you push. Extra arguments go to `golden local`
# (e.g. --fast, --mutation, --dry-run). Keep the overrides in sync with ci.yml.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run golden local --language python \
  --set typecheck-command="uv run mypy" \
  --set test-command="uv run pytest -n auto -p randomly --cov --cov-report=xml:coverage.xml --cov-report=term" \
  --set flaky-command='uv run pytest -n auto -p randomly --count="$GOLDEN_REPEATS" $GOLDEN_TESTS' \
  "$@"
