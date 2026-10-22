# Security model

A reusable workflow runs inside other people's repositories with their token. Any weakness in it
becomes a weakness in every consumer. This page lists the threats, the real incidents behind each
one, and what the pipeline does about them.

## Trust boundaries

| Actor | Trusted for | Not trusted for |
|---|---|---|
| Caller's workflow file (reviewed, on the default branch) | inputs, commands, permissions granted | — |
| Pull request code (possibly from a fork) | nothing | running with secrets, writing caches the default branch reads, publishing |
| Third-party actions and tools | the exact commit / digest / version pinned | anything a tag currently points to |
| This repository | the commit the caller pinned | a moving tag or branch |

## Threats and controls

### Repointed tags in third-party actions

- **tj-actions/changed-files (CVE-2025-30066, March 2025).** Every tag was repointed to code that
  dumped CI secrets to logs, reaching about 23,000 repositories. The entry point was
  reviewdog/action-setup (CVE-2025-30154).
- **Trivy (CVE-2026-33634, March 2026).** 76 of 77 `trivy-action` tags and all `setup-trivy` tags
  were force-pushed to an infostealer. Even SHA-pinned versions older than April 2025 were hit,
  because they downloaded trivy "latest" at runtime.

Controls:

- Every `uses:` is pinned to a 40-character commit SHA with a version comment. The scorecard's L3
  `pinned` control and zizmor's `unpinned-uses` audit enforce this.
- **Tools inside actions are pinned too:** zizmor `version: 1.30.0`, golangci-lint `v2.14.0`,
  uv `0.13.1`, the gitleaks image by digest, and every `uvx`/`go run` tool by exact version.
- Trivy is not used. Grype and Syft (Anchore) cover image scanning and SBOMs.
- Dependabot bumps SHAs with a 7-day cooldown.

### Fork code running with privileges

- **nx "s1ngularity" (August 2025).** A PR title was injected into a `pull_request_target` workflow
  and the npm token was stolen.
- **hackerbot-claw (February–March 2026).** An AI agent exploited a `pull_request_target` workflow
  in Trivy and wiped the repository.

Controls:

- No `pull_request_target` or `workflow_run` anywhere; zizmor `dangerous-triggers` audits this.
- Callers' commands reach `run:` only through environment variables, never `${{ }}` expansion
  (zizmor `template-injection`).
- `persist-credentials: false` on every checkout.

### Over-privileged tokens

- **GhostAction (September 2025, and again from August 2026).** Stolen maintainer tokens were used
  to commit workflows that exfiltrated 3,325 secrets.

Controls:

- `permissions: {}` at the top of every workflow, with each job granted the minimum
  (`contents: read` for almost all).
- Reusable workflows can only *reduce* the caller's token, never raise it.
- No `secrets: inherit`.
- Write scopes are confined to release paths: `container.yml`'s publish job (packages,
  attestations, OIDC) on pushes to the default branch, and this repository's `release.yml`
  (contents, attestations, OIDC) on version tags. The only other write is `security-events` for
  CodeQL and OpenSSF Scorecard uploads.

### Cache poisoning

- **TanStack (May 2026).** A `pull_request_target` job poisoned the pnpm store cache. The release
  job restored it, and the attacker published 84 malicious versions through trusted publishing.
- **Clinejection (March 2026).** A cache was flooded past 10 GB to evict and replace a release
  job's cache entry.

Controls:

- The publish job builds with `no-cache: true` and restores nothing.
- PR caches are scoped to the PR ref and are not visible to the default branch.
- On PRs the pipeline reads the coverage baseline (a single number) and the uv, Go and
  golangci-lint dependency caches. All of them are written only by default-branch runs, which
  PRs can read but never write. Release jobs (`container.yml` publish, `release.yml`) restore no
  cache at all.

### Malicious package versions

- **Shai-Hulud / Sha1-Hulud 2.0 (September and November 2025).** A self-propagating npm worm ran
  install scripts, stole tokens and registered victims as self-hosted runners.
- **axios 1.14.1 (March 2026).** A hijacked maintainer account shipped a remote-access trojan in a
  postinstall script.

Controls:

- Node installs run `--ignore-scripts` (or the pnpm/yarn/bun equivalent).
- Lockfiles are required (`--locked`, `npm ci`, `--frozen-lockfile`).
- Cooldowns: `uv exclude-newer = "7 days"` in the examples, and Dependabot `cooldown` (7 days, 14
  for majors).
- OSV-Scanner and dependency review run on every PR.

### Committed secrets

Controls:

- gitleaks runs from a digest-pinned image, over the PR range on PRs and the full history on
  pushes.
- GitHub secret scanning with push protection is recommended (free for public repos).

### Unsafe workflow changes in a consumer repository

Control: zizmor (`regular` persona) audits the consumer's own workflows on every run.

### Tampered artifacts

Controls:

- Images are attested with SLSA build provenance and an SBOM, signed keylessly through Sigstore.
- Consumers verify the signing workflow with `gh attestation verify --signer-workflow
  Mira190/golden-pipeline-open/.github/workflows/container.yml --deny-self-hosted-runners`.
- Builds in a reusable workflow on GitHub-hosted runners are what GitHub describes as
  helping reach SLSA Build L3, *if* verifiers enforce the signer workflow.

### Weakening the gates themselves

Controls:

- Policy floors cannot be lowered. Waivers need a reason and expire within 90 days.
- `golden.toml` and `.github/` sit behind CODEOWNERS.
- The scorecard fails any PR that removes ruleset enforcement, unpins actions or drops
  permissions blocks.

### Gate logic drifting from the YAML

Control: the CLI is installed from the *same commit* as the workflow (`$/actions/setup`), so
pinning one SHA pins both.

## Runner hardening

Every job that checks out code starts with `step-security/harden-runner` (the verdict jobs only
run `jq` over their `needs`), defaulting to `egress-policy: audit` so it
can be adopted without an allowlist. Teams that collect a baseline should switch to `block` with
an allowlist (input `egress-policy`). harden-runner is free for public repositories on
GitHub-hosted runners. It's defence in depth, not a boundary: DNS-based block-mode bypasses were
fixed in v2.16.0 (CVE-2026-32946/32947).

## Residual risks

- **This repository is itself a dependency.** Pin a SHA, review the diff on every bump, and prefer
  bumps a few days after a release.
- **Caller-supplied commands run with the caller's token.** Overriding `test-command` with
  something that fetches and executes remote code is the caller's choice and the caller's risk.
- **`harden-runner` defaults to audit.** Until you move to `block`, egress is observed, not
  prevented.
- **Self-hosted runners** (`runs-on` input) are outside this model. harden-runner is a no-op
  there without an enterprise licence, and persistent runners can carry state between jobs.

## Reporting

See [SECURITY.md](../SECURITY.md).
