# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/Mira190/golden-pipeline-open/security/advisories/new).
Do not open a public issue. You will get an acknowledgement within 3 working days.

## Supported versions

Only the latest release receives fixes. Consumers should pin a full commit SHA
(with a `# vX.Y.Z` comment) and let Dependabot propose upgrades.

## Scope

This repository ships reusable workflows that run inside other projects' CI
with their `GITHUB_TOKEN`. Anything that could let a pull request author
escalate privileges, exfiltrate secrets, poison caches or bypass a gate is in
scope. See [docs/security-model.md](docs/security-model.md).
