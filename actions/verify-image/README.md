# verify-image

Deploy-time gate for images built by [`container.yml`](../../.github/workflows/container.yml).
Run it in the job that is about to deploy. It fails the job unless the image digest has

1. SLSA build provenance (`https://slsa.dev/provenance/v1`), and
2. an SBOM attestation (`https://spdx.dev/Document/v2.3`),

both signed through Sigstore by the Golden Pipeline's `container.yml`, for the repository you
expect, on a GitHub-hosted runner. It wraps `gh attestation verify`, once per predicate type.

```yaml
deploy:
  needs: image # a job that calls container.yml
  runs-on: ubuntu-24.04
  permissions:
    attestations: read
    packages: read # only for private images
  steps:
    - uses: docker/login-action@<sha> # only for private images
      with:
        registry: ghcr.io
        username: ${{ github.actor }}
        password: ${{ github.token }}
    - uses: Mira190/golden-pipeline-open/actions/verify-image@<sha> # v1.x.y
      with:
        image: ghcr.io/acme/api # lowercase, no tag
        digest: ${{ needs.image.outputs.digest }}
    - run: ./deploy.sh "ghcr.io/acme/api@$DIGEST" # the verified digest, never a tag
      env:
        DIGEST: ${{ needs.image.outputs.digest }}
```

A complete example is in [`templates/deploy-verify.yml`](../../templates/deploy-verify.yml).

## Why

[SLSA v1.2 Build L2 and L3](https://slsa.dev/spec/v1.2/build-requirements) require that
"consumers MUST be able to validate the authenticity of the provenance". A signed attestation
nobody checks protects nothing: the signature only means something once a verifier compares the
signer's identity with the identity it expects. GitHub's guide to
[reusable workflows and Build L3](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-and-reusable-workflows-to-achieve-slsa-v1-build-level-3)
makes the same point: the build runs in a reusable workflow whose steps the calling repository
cannot change, and the verifier must pin that workflow. This action is the verifier. Neither it
nor `container.yml` makes a build "Level 3" alone; the pair gives you the checkable claim.

## What is checked

| Check | `gh attestation verify` flag | What it rules out |
|---|---|---|
| Built from this repository | `--repo` (default `github.repository`) | An image built by another repository or a fork. For a reusable workflow this is the *calling* repository. |
| Signed by the Golden Pipeline | `--signer-workflow` (default `Mira190/golden-pipeline-open/.github/workflows/container.yml`) | An image signed by the caller's own workflow, where the repository could have changed any build step. This is the pin GitHub's Build L3 guide asks verifiers to enforce. |
| Provenance and SBOM present | `--predicate-type` (once per type) | An image with a signature but no provenance, or no SBOM. |
| GitHub-hosted runner | `--deny-self-hosted-runners` (default on) | A build on a runner the signer does not control. |
| This exact digest | `oci://IMAGE@sha256:...` | A moved tag. Tags are refused. |
| Optional: ref | `--source-ref` (input `source-ref`) | A build from a feature branch. `container.yml` only publishes from the default branch, so this narrows an already narrow window. |
| Optional: signer commit | `--signer-digest` (input `signer-digest`) | Any other version of `container.yml`. `--signer-workflow` names the file, not a version. |

The flags are those of `gh attestation verify`
([manual](https://cli.github.com/manual/gh_attestation_verify)). Every input is validated before
it is used, and an empty, malformed or unexpected value is an error, not a pass.

## Inputs and outputs

| Input | Default | |
|---|---|---|
| `image` | required | Registry and name, lowercase, no tag, no digest. `container.yml` publishes `ghcr.io/<owner>/<repo>` lowercased. |
| `digest` | required | `sha256:` and 64 lowercase hex characters. |
| `repository` | `github.repository` | Repository that called `container.yml`. Set it when a separate deploy repository ships an image built elsewhere. |
| `signer-workflow` | Golden Pipeline `container.yml` | `owner/repo/path/to/workflow.yml`, no `@ref`. |
| `predicate-type` | `https://slsa.dev/provenance/v1` | Build provenance. |
| `require-sbom` | `true` | Also verify the SBOM attestation. Exactly `true` or `false`. |
| `sbom-predicate-type` | `https://spdx.dev/Document/v2.3` | `actions/attest` builds this from the SBOM's `spdxVersion`; `container.yml` publishes Syft's default, SPDX 2.3. Change it if the SBOM format changes. |
| `deny-self-hosted-runners` | `true` | Exactly `true` or `false`. |
| `source-ref` | empty | For example `refs/heads/main`. |
| `signer-digest` | empty | Full commit SHA of the pinned `container.yml`. |
| `token` | `github.token` | Token that reads attestations. |

Output `verified` is `true` when every check passed. On failure the step fails and the output is
never set, so do not wrap the step in `continue-on-error`. A job summary line records what was
verified.

## What the caller must grant

- **`attestations: read`** on the deploy job. `gh attestation verify` fetches the attestations
  from the GitHub API with the token, and GitHub documents `attestations: read` as the permission
  for that API. Nothing else is needed for a public image built in the same repository.
- **`packages: read`**, plus a `docker/login-action` step for `ghcr.io`, when the image is
  private: `gh` must be authenticated with the registry to resolve `oci://` references.
- **A different `token`** when `repository` is a different private repository, for example a
  fine-grained token with read access to that repository's attestations. Pass it as a secret.
- Not needed: `contents`, `id-token` or any write scope. Set `permissions: {}` at the top of the
  workflow and grant only these on the deploy job.

`gh` is preinstalled on GitHub-hosted runners. On a self-hosted runner, install it first (the
action fails if it is missing). The job needs network access to the GitHub API, the registry,
and the hosts `gh` uses to fetch the Sigstore trusted root. If you run `harden-runner` in
`block` mode, take the allowlist from its audit report.

## Limits

- Verification proves where the image was built and by which workflow. It does not prove the
  source was reviewed, or that the image is free of vulnerabilities (`container.yml` scans before
  it publishes, and the SBOM lets you rescan later).
- `--signer-workflow` pins a file, not a version. Pin `container.yml` by SHA in the calling
  workflow, and set `signer-digest` to that SHA if you want the verifier to enforce it too.
- The contents of the attestation's predicate are written by the build. Only the signing
  certificate says who built the image; this action checks the certificate.

## Break-glass: deploying without verification

There is deliberately no `skip` input. A flag in the workflow would let anyone with write access
turn the check off in a pull request, silently, which is exactly the failure the check exists to
prevent. If production is down and the pipeline cannot produce a verifiable image, deploy through
a separate, visible path instead, and treat it as an exception with a record and an end date:

- A dedicated `workflow_dispatch` workflow, deploying to an environment with required reviewers
  (and CODEOWNERS on `.github/`, so it can only be changed by review).
- Required inputs: the digest, the reason, and a ticket or incident link. Write all three to the
  job summary and to your incident record.
- The exception expires. Within a fixed window (for example 24 hours), rebuild through the
  pipeline and redeploy the verified digest, or record who accepted the risk and until when. Do
  not leave the unverified image running because it works.
- Never edit this action's inputs to `false`, add `continue-on-error`, or comment out the step to
  "get it through". The same applies to the other gates: waivers here need a reason and an expiry.

## Troubleshooting

- **`digest must be an image digest`**: the `container.yml` job did not publish, so its `digest`
  output is empty. It publishes only on a push to the default branch.
- **`no build provenance attestation ... verified`**: read the `gh` output above the error. The
  usual causes are a wrong `repository` (it is the caller, not the pipeline repository), a
  different `signer-workflow`, a build on a self-hosted runner, or an image that was never
  attested.
- **SBOM fails but provenance passes**: check `sbom-predicate-type` against the SBOM's
  `spdxVersion`. `actions/attest` turns `SPDX-2.3` into `https://spdx.dev/Document/v2.3`.
- **`gh` cannot reach the registry**: a private image needs the login step above.
