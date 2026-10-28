# Adoption

## 1. Add the workflow

Copy a template from [`templates/`](../templates) into `.github/workflows/ci.yml` and pin the
reusable workflow to a full commit SHA:

```yaml
jobs:
  ci:
    uses: Mira190/golden-pipeline-open/.github/workflows/golden.yml@<40-char-sha> # v1.0.0
    permissions:
      contents: read
    with:
      language: python
```

Dependabot (`package-ecosystem: github-actions`) keeps the SHA and the `# v1.0.0` comment up to
date. One bump upgrades the YAML and the gate logic together.

To find tests that turn flaky after they merge, also copy
[`templates/ci-scheduled.yml`](../templates/ci-scheduled.yml) to
`.github/workflows/flaky-scan.yml`, pin the same SHA and set `language`. It runs nightly on the
default branch.

- Don't make it a required check: it never runs on pull requests.
- GitHub disables scheduled workflows after 60 days without repository activity.

## 2. Configure the repository

**Ruleset.** Settings → Rules → Rulesets → New branch ruleset, targeting the default branch:

- **Require status checks to pass:** add `ci / golden` (the caller job's name, then `golden`).
  Restrict the source to *GitHub Actions*. Don't list the individual jobs.
- **Require a pull request before merging:** 1 approval, require review from Code Owners,
  dismiss stale approvals on push, and require approval of the most recent push (the last
  pusher can't approve their own code: two-person review).
- **Block force pushes** and **Restrict deletions.**
- **Require merge queue**, if available for your plan. The workflow already listens to
  `merge_group`.
- No bypass list. Exceptions belong in `golden.toml` waivers, where they are visible and expire.

**Security settings** (free for public repositories):

- the **dependency graph**. The security job's dependency review needs it, and without it every
  pull request fails with "Dependency review is not supported on this repository" (Settings →
  Advanced Security, or Code security, depending on your plan)
- secret scanning with push protection
- Dependabot alerts
- private vulnerability reporting
- **Immutable releases**, if you publish releases yourself

**Organisation policy:**

- **Require the pipeline centrally.** On a `pull_request` event GitHub runs the pull request's own
  copy of the caller workflow, so a PR can edit the very `ci.yml` that judges it (`golden plan`
  flags this on the file, but a warning is not a control). Put the caller workflow in a
  governance repository, then add an *organisation* ruleset with **"Require workflows to pass
  before merging"** that targets every adopting repository and points at that file. GitHub then
  runs the governance repository's version, which no pull request can edit. The workflow must
  listen to `pull_request` and `merge_group`. Where organisation rulesets aren't available,
  require Code Owner review of `.github/workflows/` instead (the scorecard's L3 `owner-review`
  control checks this).
- Actions → General → "Require actions to be pinned to a full-length commit SHA".
- Allow only GitHub-owned and verified actions, plus `Mira190/golden-pipeline-open`.

## 3. Roll out without stopping the team

Turning everything on in an old repository makes every PR red, and teams learn to work around
red. Ratchet in instead:

| Week | Step |
|---|---|
| 1 | Add the workflow. Add waivers (≤ 90 days) for the gates you can't meet yet, e.g. `mutation` on a legacy codebase. Everything reports; nothing new blocks. |
| 2 | Require `ci / golden` in the ruleset. Diff coverage and test presence apply only to *changed* lines, so legacy code doesn't block anyone, but new code must meet the bar. |
| 3–6 | Let each waiver expire. Fix surviving mutants as you touch the code. Add `presence.exempt` globs for generated code. |
| 6+ | Raise `scorecard.min_level` to 3. Consider stricter thresholds per project. |

A waiver is the only way to go below the floor. It needs a reason and an expiry, and it blocks
L4, so exceptions stay visible to the team and to whoever audits the repository.

## 4. Monorepos

Call the workflow once per project, each with its own `working-directory`:

```yaml
jobs:
  api:
    uses: Mira190/golden-pipeline-open/.github/workflows/golden.yml@<sha> # v1.0.0
    permissions: { contents: read }
    with: { language: go, working-directory: services/api }
  web:
    uses: Mira190/golden-pipeline-open/.github/workflows/golden.yml@<sha> # v1.0.0
    permissions: { contents: read }
    with: { language: node, working-directory: apps/web }
```

Diffs, reports, artifact names, cache keys and annotations are all scoped by directory. Require
each `<job> / golden` check, or add one aggregating job of your own (see this repository's
`ci.yml`). Each project can carry its own `golden.toml`.

## 5. Shift left: the same verdict before you push

A red pipeline after a 10-minute wait is the most expensive place to learn about a problem.
`golden local` runs the effective commands and gates from `golden.yml` on your machine, against
the merge-base with the default branch. It runs the same code, so it reaches the same verdict:

```bash
uvx --from git+https://github.com/Mira190/golden-pipeline-open@<sha> golden local --language python
golden local --language node --fast      # policy, format, lint, types: seconds
golden local --language go --mutation    # also the mutation gate on changed code
golden local --language python --dry-run # show the commands without running them
```

It reads overrides from `--set key=value`, or from `GOLDEN_INPUTS` (the same JSON the workflow
passes), so a customised `test-command` behaves the same locally.

To run it automatically, use [pre-commit](https://pre-commit.com) with `rev:` set to the same SHA
your `ci.yml` pins:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/Mira190/golden-pipeline-open
    rev: <40-char-sha> # v1.x.y, same as ci.yml
    hooks:
      - id: golden-policy
      - id: golden-fast
        args: [--language, python]
      - id: golden-full # pre-push: tests and diff gates
        args: [--language, python]
```

**Coding agents.** Code written by agents goes through the same gates. Tell the agent to run
`golden local` before it pushes, for example in an `AGENTS.md` or `CLAUDE.md` (see this
repository's own `AGENTS.md`). The agent then gets the pipeline's verdict while it still has the
context to fix it.

## 6. Containers and progressive delivery

Add `container.yml` for services that ship as images:

```yaml
  image:
    needs: ci
    uses: Mira190/golden-pipeline-open/.github/workflows/container.yml@<sha> # v1.0.0
    permissions:
      contents: read
      packages: write
      id-token: write
      attestations: write
    with:
      smoke-command: docker run --rm "$IMAGE" --version
```

### Progressive delivery

On the default branch the image is pushed to GHCR with SLSA build provenance and an SBOM
attestation, both signed keylessly through Sigstore. Your deploy step should refuse anything this
workflow didn't build:

```bash
gh attestation verify "oci://ghcr.io/OWNER/IMAGE@$DIGEST" --owner OWNER \
  --signer-workflow Mira190/golden-pipeline-open/.github/workflows/container.yml \
  --source-ref refs/heads/main --deny-self-hosted-runners
```

Roll out the verified digest progressively, with automated analysis at each step and automatic
rollback (for example Argo Rollouts or Flagger with SLO queries). Use small steps (one cell or 5%
first), bake times that grow with exposure, and *no exempt change types*: configuration and content
go through the same rings. That is the lesson of the July 2024 CrowdStrike outage. The deployment
mechanism is environment-specific, so this repository stops at a verifiable artifact.

## 7. Customising commands

Any step can be replaced without forking:

```yaml
    with:
      language: node
      test-command: pnpm vitest run --coverage.enabled --coverage.reporter=cobertura
      flaky-command: pnpm vitest run --repeats="$GOLDEN_REPEATS" $GOLDEN_TESTS  # Vitest 5+
```

A replacement must keep the contract: write the coverage report to `coverage-report`, honour
`$GOLDEN_TESTS` and `$GOLDEN_REPEATS`, and write the mutation report in a supported format.
