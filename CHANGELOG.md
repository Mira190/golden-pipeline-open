# Changelog

## 1.0.0 — 2026-10-21

First release.

- `golden.yml`: reusable pipeline for Python (uv), TypeScript/Node and Go with a single
  required `golden` check.
- Gates: diff coverage, test presence, coverage ratchet, flake re-runs, diff-scoped mutation
  testing (mutmut / StrykerJS / gremlins), repository scorecard.
- `security.yml`: gitleaks, OSV-Scanner, dependency review, zizmor.
- `container.yml`: build, smoke test, SBOM, grype, signed SLSA provenance and SBOM attestations.
- Policy as code (`golden.toml`) with platform floors and expiring waivers.
- Gate-effectiveness drills (`scripts/drill.py`) running in CI.
