# Landing site release CI/CD (OIDC, least-privilege)

## Branch
chore/landing-release-ci

## Goal
Every merge to `main` that changes `infra/landing/**` deploys automatically:
`terraform apply` runs in CI via a short-lived, OIDC-assumed AWS role, then the
CloudFront cache is invalidated. No human ever runs `terraform apply` for a
routine content change, and no long-lived AWS credential or manually-pasted
secret is ever stored in GitHub.

## Non-goals
- Does not change the landing site's content, Terraform-managed resources
  (bucket/CloudFront/ACM/DNS), or the manual GoDaddy DNS steps in
  `infra/landing/README.md` — those stay exactly as documented.
- Does not attempt to make CI able to apply *every* possible change to this
  stack (e.g. CloudFront distribution config, ACM cert, bucket policy). Those
  stay a manual, human-run `terraform apply` under the operator's own SSO
  role — see "Fail-closed by design" below. This plan only automates the
  content-publish path the README already documents as routine.
- Does not touch `infra/admin`'s existing release role/workflow — this is a
  new, separate role and a new, separate workflow file.
- Does not set the resulting GitHub Actions variable through this plan's
  `/execute-feature` run. That requires the role to exist first (a
  chicken-and-egg the admin-console role had too), so it happens once, by
  hand, after this PR merges — see Acceptance Criteria.

## Evidence
- `infra/admin/main.tf` (merged `ff2e9dc`, already in production use) is the
  proven reference pattern for this exact problem on the admin console: OIDC
  provider (existing, account-wide — confirmed via `aws iam
  list-open-id-connect-providers`, ARN
  `arn:aws:iam::839287955684:oidc-provider/token.actions.githubusercontent.com`),
  a role trust-scoped to one repo + one branch, and a role policy scoped to
  exactly the actions the release needs on exactly the resources it touches.
  `admin-ui`'s own commit message documents the failure mode this pattern
  fixes: "a PR merged, deployed green, and left the console serving a stale
  bundle... hand-released three times in one day before this landed."
- `infra/landing/main.tf`'s `aws_s3_object.site` already uploads the entire
  `site/` fileset by content hash on every `terraform apply` — this is
  exactly the RCA finding from `plans/260821_landing-brand-refresh-reconcile.md`:
  a `terraform apply` run from the wrong checkout silently overwrote a
  deployed-but-unmerged branch's content. Automating the apply on merge (and
  only on merge to `main`) removes the "which checkout is this being run
  from" question entirely — CI only ever applies what's actually on `main`.
- `infra/landing/backend.tf`: state lives in the shared
  `vibesense-tfstate-839287955684` bucket, key `landing/terraform.tfstate`,
  S3-native locking (`use_lockfile = true`, no DynamoDB table to grant access
  to).
- `infra/backend-bootstrap/create-state-bucket.sh`: the state bucket has no
  bucket-level IAM policy restricting by key prefix today — access control for
  this plan's role is entirely at the IAM policy layer (below), not the
  bucket policy layer.
- No CI/CD exists for this site today — confirmed exhaustively (this session)
  across every `.yml`/`.tf`/Caddyfile in all three repos, every branch
  (merged and unmerged) in `cs-server` and `vibes_bot`, and every worktree.
  `vibes_bot/.github/workflows/main.yml`'s `Release-Admin-Console` job is a
  different bucket (`vibesense-admin-*`) and a different distribution
  (`E3W5DJI8L1TDCO`) — unrelated to this one
  (`vibesense-landing-839287955684` / `E2CXH3ELT8Z2FD`).
- `gh variable list --repo mcbsf/vibes_bot` shows `ADMIN_UI_RELEASE_ROLE_ARN`
  is a **variable**, not a secret — role ARNs are not sensitive on their own
  (the OIDC trust condition, not secrecy of the ARN, is what gates
  assumption). This plan follows the same choice.

## Current Behavior
`infra/landing/README.md`'s documented "Updating the site later" runbook is
100% manual: an operator with the `vibesense` AWS SSO profile runs
`cd infra/landing && terraform apply` and, optionally,
`aws cloudfront create-invalidation` by hand, from whatever branch happens to
be checked out locally at that moment.

## Desired Behavior
On every push to `main` that touches `infra/landing/**`:
1. GitHub Actions assumes a dedicated IAM role via OIDC (no stored
   credential).
2. `terraform apply -auto-approve` runs against the existing
   `infra/landing` stack, from a clean checkout of `main` — so what CI applies
   is always exactly what's on `main`, never a stray local branch.
3. CloudFront is invalidated (`/*`) so the change is visible immediately.
4. If the merged change requires an action outside the role's granted
   permissions (see "Fail-closed by design"), the job fails loudly with
   `AccessDenied`, and the README's manual runbook is still there as the
   fallback for that case.

A `workflow_dispatch` trigger allows re-running the same job by hand (e.g. to
re-apply after a manual out-of-band fix, or to verify the pipeline after
first standing it up).

## Implementation Approach

### 1. New file `infra/landing/ci.tf` (kept separate from `main.tf` so the
   site-hosting resources and the CI-release resources review independently)

- `data "aws_iam_openid_connect_provider" "github"` — reuse the existing
  account-wide provider (do not create a second one; `aws_iam_role` is the
  only resource that needs to be new).
- `resource "aws_iam_role" "landing_release"`:
  - `name = "vibesense-landing-release"`
  - `max_session_duration = 3600`
  - Trust policy: `sts:AssumeRoleWithWebIdentity` from the GitHub OIDC
    provider, `Condition.StringEquals` on
    `token.actions.githubusercontent.com:aud = "sts.amazonaws.com"` **and**
    `token.actions.githubusercontent.com:sub =
    "repo:${var.release_repository}:ref:refs/heads/${var.release_branch}"`
    — identical shape to `infra/admin/main.tf`'s `console_release` role.
- `resource "aws_iam_role_policy" "landing_release"` — least privilege, split
  into a *write* set (exactly what a content release needs) and a *read* set
  (what `terraform apply`'s state refresh needs to succeed without erroring,
  even though it can't write those resources):
  - **Write — site content:** `s3:PutObject`, `s3:DeleteObject` on
    `arn:aws:s3:::vibesense-landing-<account_id>/*`.
  - **Write — cache:** `cloudfront:CreateInvalidation` on
    `aws_cloudfront_distribution.site.arn` only.
  - **Write — Terraform state:** `s3:GetObject`, `s3:PutObject`,
    `s3:DeleteObject` on
    `arn:aws:s3:::vibesense-tfstate-<account_id>/landing/*` (covers both
    `landing/terraform.tfstate` and its S3-native lock object) — scoped to
    the `landing/` prefix only, so this role structurally cannot touch
    `rds/terraform.tfstate` or any other stack's state, preserving the exact
    isolation `backend.tf`'s own comment calls out.
  - **Read — list for sync/lock diffing:** `s3:ListBucket` on the landing
    site bucket and on the tfstate bucket (the latter condition-scoped to
    `s3:prefix = "landing/"`).
  - **Read only, no write — everything else `terraform plan`'s refresh
    touches:** `s3:GetBucket*` on the site bucket;
    `cloudfront:GetDistribution`, `cloudfront:GetDistributionConfig`,
    `cloudfront:ListDistributions`, `cloudfront:GetOriginAccessControl` on
    the landing distribution/OAC; `acm:DescribeCertificate`,
    `acm:ListCertificates` on the landing cert. Deliberately **absent**:
    `cloudfront:UpdateDistribution`, `s3:PutBucketPolicy`,
    `s3:PutBucketPublicAccessBlock`, `acm:RequestCertificate`,
    `acm:DeleteCertificate`, and anything Route53/GoDaddy-adjacent.
- `variables.tf` additions: `release_repository` (default
  `"marlonreghert/cs-server"`), `release_branch` (default `"main"`) — same
  variable names and shape as `infra/admin/variables.tf`.
- `outputs.tf` addition: `output "landing_release_role_arn"` (the value the
  operator pastes — actually, sets via `gh variable set`, see Acceptance
  Criteria — into the `LANDING_RELEASE_ROLE_ARN` GitHub Actions variable).

### 2. New file `.github/workflows/landing-deploy.yml` (in `cs-server`, not
   `vibes_bot` — the landing site's Terraform, state, and static content are
   100% self-contained in `cs-server`, so this workflow has no reason to
   check out a second repository, unlike `vibes_bot`'s admin-console job
   which builds a React app that lives in `vibes_bot`)

- Trigger: `push` to `main` with `paths: ['infra/landing/**']`, plus
  `workflow_dispatch` for manual re-runs.
- `permissions: { id-token: write, contents: read }` — nothing else.
- `concurrency: { group: landing-deploy, cancel-in-progress: false }` — queue
  rather than race two merges that land close together (belt-and-suspenders;
  S3-native state locking would also block a true concurrent apply, but
  queuing avoids a failed/retried run).
- Steps: checkout `main`; `aws-actions/configure-aws-credentials@v4` with
  `role-to-assume: ${{ vars.LANDING_RELEASE_ROLE_ARN }}`,
  `aws-region: us-east-1`; `terraform init` + `terraform apply -input=false
  -auto-approve` in `infra/landing`; `aws cloudfront create-invalidation
  --distribution-id <output> --paths '/*'` reading the distribution ID from
  `terraform output`.
- Comment block at the top of the file explaining the fail-closed design (see
  below), so a future maintainer who sees this job fail on a legitimate infra
  PR understands why and knows the manual README runbook is the correct
  fallback for that case — not a bug to work around by widening the role.

### Fail-closed by design
The role's write permissions cover exactly: site-bucket objects, one
CloudFront invalidation, and its own state-key prefix. A `main.tf` change
that alters the CloudFront distribution config, the bucket policy, or the ACM
certificate will make `terraform apply` fail with `AccessDenied` in CI — by
design, not by omission. That failure is the intended guardrail: CI may
automate *content* releases; changes to the stack's *topology* still require
a human running `terraform apply` under their own SSO admin role, exactly as
today. This is deliberately narrower than giving CI the same access as the
human operator.

## Data, Config, And API Impact
None — no RDS schema, Redis projection, or API contract changes. Terraform
and GitHub Actions configuration only.

## Error Handling And Observability
- A failed `terraform apply` in CI fails the GitHub Actions job (red X on the
  commit) — the existing GitHub notification surface is the alert; no new
  Prometheus metric is warranted for a low-frequency (few times a month),
  human-visible-by-construction CI job.
- `AccessDenied` on anything beyond the granted write set is the intended
  failure mode for topology changes (see above), not a defect to silence.

## Test Plan
`# bdd-exempt: Terraform + GitHub Actions configuration, outside the FastAPI
app and its pytest/behave harness — same exemption basis as
plans/260706_landing-site-and-tfstate-s3.md and
plans/260821_landing-brand-refresh-reconcile.md.`

Manual or integration checks (must pass before merge):
- `terraform validate` and `terraform plan` in `infra/landing` show only the
  new IAM resources being added (`0 to change, 0 to destroy` on every
  existing resource) — confirms the new role/policy can't accidentally touch
  the live site or its state.
- Review the IAM policy JSON line-by-line against the least-privilege list
  above before merge — no wildcard resources (`"*"`), no unlisted write
  actions.
- Confirm the workflow's trust condition repository/branch values match
  `marlonreghert/cs-server` / `main` exactly (a typo here would either lock
  CI out entirely — safe failure — or, worse, scope it too broadly).

Post-merge verification (part of `/execute-feature`'s deploy step, run by the
orchestrating session, not the implementing subagent):
- One-time manual `terraform apply` (human SSO role) to create the new IAM
  role/policy — CI cannot bootstrap its own role.
- Set the `LANDING_RELEASE_ROLE_ARN` GitHub Actions **variable** on
  `marlonreghert/cs-server` from the `terraform output`, via `gh variable
  set` — not a manual paste into the GitHub UI.
- Trigger the new workflow once via `workflow_dispatch` and confirm it
  succeeds (expect `0 to change` — proves OIDC assumption and the granted
  permissions work end-to-end without needing a real content change).

## Acceptance Criteria
- `infra/landing/ci.tf`, `variables.tf`, `outputs.tf`, and
  `.github/workflows/landing-deploy.yml` exist on the feature branch; PR
  opened against `main`, not merged by the implementing session.
- IAM policy contains no wildcard resource and no write action outside the
  list in "Implementation Approach" — verified by review before merge.
- After merge: the role exists in AWS, `LANDING_RELEASE_ROLE_ARN` is set on
  the `cs-server` repo, and a `workflow_dispatch` run of
  `landing-deploy.yml` completes successfully.
- A subsequent content-only change merged to `main` (e.g. any future
  `infra/landing/site/*` edit) deploys without a human running `terraform
  apply`.

## Open Questions
None.
