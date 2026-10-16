# Reusable container workflows

Shared GitHub Actions workflows extracted from AgenticCloudSolutions. Callers
keep their triggers, service/variant matrices, lint and dependency checks, and
staging approval gates. These workflows check out the **calling repository**.

## Build, scan, and publish

`.github/workflows/build-scan-push.yml` lints with hadolint 2.14.0, builds once,
scans locally with Trivy, then independently attempts GHCR and/or ECR. Publishing
requires a successful scan. Fixable HIGH/CRITICAL vulnerabilities block publishing;
SARIF upload is best effort, matching the original pipelines.

Tags are `sha-<git short SHA>`, `latest`, and the repository's version. A successful
push to either enabled registry passes the publish gate; a missing enabled copy
produces a warning. With only one registry enabled, that registry must succeed.
Only the current build's three tags are pushed, including on persistent runners.

```yaml
jobs:
  image:
    permissions:
      contents: read
      packages: write
      security-events: write
      id-token: write
    uses: Cloud-Infrastructure-as-Code-Solutions/github-actions/.github/workflows/build-scan-push.yml@main
    with:
      dockerfile: services/api/infrastructure/docker/Dockerfile
      image-name: example-api
      cache-scope: api
      publish-ecr: true
      aws-role-arn: ${{ vars.AWS_CI_ROLE_ARN }}
      ecr-repository: example/api
```

Required inputs are `dockerfile`, `image-name`, and `cache-scope`. GHCR defaults
to enabled; ECR defaults to disabled. Set `publish-ghcr: false` for ECR-only builds.
`image-name` excludes the GHCR hostname and owner; the caller's owner is lowercased.
ECR repositories must already exist. `ecr-registry` defaults to the authenticated
account's registry. An explicit hostname must match a registry authenticated by
the ECR login action; this workflow logs in to the assumed role's account.

Optional build inputs: `context` (`.`), `runner` (`ubuntu-latest`), `build-args`
(newline-separated non-secret arguments), `version-file` (`VERSION`),
`version-format` (`text`, or `json` to read `.version`), and `trivy-ignore-file`
(`.trivyignore`). Give each service/variant a distinct `cache-scope`.
Use `build-contexts` for newline-separated named BuildKit contexts, such as
`content=./content` for plAIbook's UI media. It is forwarded to both the local
scan build and optional multi-platform publish builds.
Set `platforms: linux/amd64,linux/arm64` to publish multi-architecture manifests,
as ACS requires for its Graviton EC2 host. This enables QEMU, scans a local build
for the runner architecture, then builds and pushes each registry's platform
manifest independently using the shared cache. The additional architectures are
not scanned individually. Leaving `platforms` empty publishes the scanned local
image directly, as AEP does.
The runner needs Docker, bash, git, and jq. AWS region defaults to `us-east-1`.

Pass BuildKit secrets explicitly, including across organizations:

```yaml
    secrets:
      build-secrets: |
        gh_token=${{ secrets.GITHUB_TOKEN }}
```

The caller's `GITHUB_TOKEN` authenticates GHCR automatically. Outputs are
`sha_short`, `ghcr-published`, and `ecr-published`; registry outputs contain the
push step's outcome (`success`, `failure`, or `skipped`). Matrix callers receive
the last successful invocation's outputs, so use `sha_short` across the matrix
only when every entry checks out the same caller commit, as ACS and AEP do.

ACS keeps its UI variants: full content goes to GHCR, while the API gateway
variant goes to ECR. AEP uses `package.json` and forwards its private-package
BuildKit secret and repository URL argument; its Helm deployments stay in AEP.

## Deploy a Docker service on EC2

`.github/workflows/deploy-ec2.yml` authenticates with AWS OIDC and invokes an
existing SSM document accepting `Service` and `ImageTag` string parameters. The
host document owns Docker Compose, image pulls, configuration, and health checks;
the workflow only requests a rollout and waits for the command result. It neither
bootstraps the host nor requires SSH keys.

```yaml
  deploy:
    needs: [image, stage-tests]
    permissions:
      contents: read
      id-token: write
    uses: Cloud-Infrastructure-as-Code-Solutions/github-actions/.github/workflows/deploy-ec2.yml@main
    with:
      service: api
      image-tag: sha-${{ needs.image.outputs.sha_short }}
      aws-role-arn: ${{ vars.AWS_CI_ROLE_ARN }}
      ssm-document: ${{ vars.AWS_SSM_DEPLOY_DOCUMENT }}
      instance-id: ${{ vars.AWS_APP_INSTANCE_ID }}
```

All six inputs shown are required. Optional inputs are `aws-region` (`us-east-1`),
`runner` (`ubuntu-latest`), and `timeout-seconds` (`900`, integer 1–1500). The output
is `command-id`. The runner needs AWS CLI, bash, and jq. The workflow tolerates SSM
eventual consistency, reports remote stdout/stderr, and fails on command failure,
cancellation, timeout, or an AWS API error. The job has a 30-minute upper limit.

Keep `max-parallel: 1` on deployment matrices sharing one Compose host. A concurrency
group also serializes jobs from different runs in the same calling repository for
that region/instance. GitHub keeps at most one pending job per concurrency group;
new pending jobs can supersede older pending jobs. This is not a cross-repository
lock: use a host-side lock if multiple repositories deploy to the same instance.

## Adoption and access

1. Publish this repository's `main` branch **before** merging either consumer.
   The callers currently reference `@main` because this repository started empty.
   Pin callers to a published commit SHA for immutable workflow versions.
2. Make these workflows accessible to both caller organizations. Public visibility
   supports cross-organization calls; a private repository requires a supported
   enterprise sharing configuration. Allow these workflows and their referenced
   actions in the callers' Actions policies.
3. Keep the existing repository variables in ACS: `AWS_CI_ROLE_ARN`, `AWS_REGION`,
   `AWS_ECR_REGISTRY`, `AWS_SSM_DEPLOY_DOCUMENT`, and `AWS_APP_INSTANCE_ID`.
   AWS identity, repositories, instance, deploy document, and host setup remain
   externally provisioned. No AWS variables are needed by AEP's GHCR-only caller.
4. The AWS role's OIDC trust must allow the calling repository/ref, including any
   `job_workflow_ref` conditions restricting reusable workflow identity. Preserve
   the existing ECR push and SSM deployment permissions; the CI role should only
   invoke its deploy document and target instance, not the bootstrap document.

GitHub's [reusable workflow documentation](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
describes input, secret, permission, matrix output, and access behavior.

## Local validation

Run `actionlint` against `.github/workflows/*.yml`. For behavior checks, install
PyYAML and ensure bash and jq are on PATH, then run:

```sh
python -m unittest discover -s tests -v
```

On Windows set `BASH` to the Git Bash executable. Tests run the actual workflow
shell programs with fake Docker/AWS commands; they cover version sources, tags,
independent registry failures, ECR retagging, SSM states and API failures, eventual
consistency, and wait expiry. They do not publish images or deploy infrastructure.
