# ----------------------------------------------------------------------------
# GitHub Actions release role — lets CI publish the landing site's content
# and apply this stack's own Terraform, without any stored AWS credential.
#
# This site's Terraform lived in this repo since it was created, but every
# publish was a human running `terraform apply` by hand from whatever branch
# happened to be checked out locally — which is exactly how a `terraform
# apply` from the wrong checkout once silently overwrote a colleague's
# already-deployed-but-unmerged branch (see
# plans/260821_landing-brand-refresh-reconcile.md). Automating the apply on
# merge to `main` removes the "which checkout is this being run from"
# question entirely: CI only ever applies what's actually on `main`.
#
# OIDC, not an access key: nothing long-lived is stored in GitHub, so there is
# no secret to leak or rotate. The trust condition pins BOTH the repository
# and the branch, so a fork, a pull request from a fork, or any other ref
# cannot assume this role. Mirrors infra/admin/main.tf's `console_release`
# role/policy trust-policy shape exactly — same OIDC pattern, adapted to this
# stack's own bucket, distribution, and state-key prefix.
#
# Unlike admin's console_release role, this role's job runs a REAL
# `terraform apply` (not just `aws s3 sync`), so it also needs enough
# read-only access for `terraform`'s state refresh to succeed on every
# resource already in this stack's state — that read set has no precedent to
# copy from admin (admin's job never invokes terraform at all).
# ----------------------------------------------------------------------------
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# Same shared state bucket as backend.tf, built the same way main.tf builds
# this stack's own bucket name — from the account id already fetched by
# data.aws_caller_identity.current — rather than hardcoding the account
# number a second time in this file.
locals {
  tfstate_bucket_name = "vibesense-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_iam_role" "landing_release" {
  name        = "vibesense-landing-release"
  description = "GitHub Actions OIDC: publish the landing site and apply its own Terraform state. Scoped to ${var.release_repository} on ${var.release_branch}."
  tags        = var.tags

  # One hour: a release takes seconds, and a short session limits how long a
  # leaked token is useful.
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.release_repository}:ref:refs/heads/${var.release_branch}"
        }
      }
    }]
  })
}

# Least privilege, fail-closed by design: this policy grants write access to
# exactly three things — the site bucket's objects, this one distribution's
# invalidations, and this stack's own state-key prefix (`landing/` only, in
# the bucket shared with every other stack's state) — plus enough READ access
# for `terraform apply`'s refresh to succeed on the resources it doesn't own.
#
# Deliberately absent: any write action on the CloudFront distribution config,
# the ACM certificate, the bucket policy, or the public-access-block. A
# `main.tf` change to any of those makes `terraform apply` fail in CI with
# `AccessDenied` — that is the intended guardrail, not a bug. CI may automate
# routine *content* releases; changes to the stack's *topology* still require
# a human running `terraform apply` under their own SSO admin role, exactly as
# documented in README.md today.
resource "aws_iam_role_policy" "landing_release" {
  name = "landing-release"
  role = aws_iam_role.landing_release.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ------------------------------------------------------------------
      # WRITE — site content
      # ------------------------------------------------------------------
      {
        Sid      = "WriteSiteContent"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.site.arn}/*"
      },
      # s3:GetObject (HeadObject, IAM-action-equivalent) is needed so
      # terraform's own `aws_s3_object` resources can compare remote ETags on
      # every refresh — this is NOT the admin-console pattern (that role
      # deliberately omits GetObject because its release step is a shell
      # `aws s3 sync`, which diffs via ListBucket metadata only). Landing's
      # release step is a real `terraform apply` against `aws_s3_object`
      # resources, which reads objects individually. Read-only, and scoped to
      # the exact same object set this role can already overwrite — it adds
      # no new resource exposure.
      {
        # s3:GetObjectTagging is a separate action from s3:GetObject (object
        # tags are a distinct API), needed for the same per-object drift
        # refresh as GetObject above.
        Sid      = "ReadSiteContentForDriftDetection"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectTagging"]
        Resource = "${aws_s3_bucket.site.arn}/*"
      },

      # ------------------------------------------------------------------
      # WRITE — cache invalidation, this distribution only
      # ------------------------------------------------------------------
      {
        Sid      = "InvalidateLandingOnly"
        Effect   = "Allow"
        Action   = "cloudfront:CreateInvalidation"
        Resource = aws_cloudfront_distribution.site.arn
      },

      # ------------------------------------------------------------------
      # WRITE — this stack's own Terraform state key prefix only
      # ------------------------------------------------------------------
      {
        # Covers both landing/terraform.tfstate and its S3-native lock
        # object. Scoped to the landing/ prefix only, so this role
        # structurally cannot read or write rds/terraform.tfstate or any
        # other stack's state — the same isolation backend.tf's own comment
        # calls out.
        Sid      = "ReadWriteOwnStateKeyOnly"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${local.tfstate_bucket_name}/landing/*"
      },

      # ------------------------------------------------------------------
      # READ — list, for sync/lock diffing
      # ------------------------------------------------------------------
      {
        Sid      = "ListSiteBucketForSyncDiff"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.site.arn
      },
      {
        Sid      = "ListStateBucketForLandingPrefixOnly"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:aws:s3:::${local.tfstate_bucket_name}"
        Condition = {
          StringLike = {
            "s3:prefix" = "landing/*"
          }
        }
      },

      # ------------------------------------------------------------------
      # READ ONLY, NO WRITE — everything else terraform plan's refresh
      # touches. No s3:PutBucketPolicy, no s3:PutBucketPublicAccessBlock —
      # this role cannot change who else can read the bucket or how it's
      # protected.
      # ------------------------------------------------------------------
      {
        # s3:GetBucket* covers most bucket-level reads (Acl, Cors, Logging,
        # Policy, RequestPayment, Tagging, Versioning, Website, ...), but four
        # S3 IAM action names break that naming convention and must be listed
        # explicitly — confirmed empirically via a full `terraform plan`
        # under TF_LOG=debug, cross-checking every AWS API call made against
        # this policy: GetBucketAccelerateConfiguration's IAM action is
        # `GetAccelerateConfiguration` (no "Bucket"), and likewise
        # GetEncryptionConfiguration, GetLifecycleConfiguration, and
        # GetReplicationConfiguration.
        Sid    = "ReadSiteBucketMetadata"
        Effect = "Allow"
        Action = [
          "s3:GetBucket*",
          "s3:GetAccelerateConfiguration",
          "s3:GetEncryptionConfiguration",
          "s3:GetLifecycleConfiguration",
          "s3:GetReplicationConfiguration",
        ]
        Resource = aws_s3_bucket.site.arn
      },
      {
        # No cloudfront:UpdateDistribution — this role cannot change the
        # distribution's config, only read it (required for refresh).
        Sid      = "ReadDistributionOnly"
        Effect   = "Allow"
        Action   = ["cloudfront:GetDistribution", "cloudfront:GetDistributionConfig"]
        Resource = aws_cloudfront_distribution.site.arn
      },
      {
        Sid      = "ReadOriginAccessControlOnly"
        Effect   = "Allow"
        Action   = "cloudfront:GetOriginAccessControl"
        Resource = aws_cloudfront_origin_access_control.site.arn
      },
      {
        # main.tf's `aws_cloudfront_function.v_rewrite` (the /v/<id> smart-
        # link rewrite) is refreshed on every apply like any other resource
        # in state. Not called out by name in the plan's read-only list, but
        # required for the same reason the distribution/OAC reads are: without
        # it, EVERY apply — not just a topology change — would fail refresh,
        # which is a functional gap, not the intended fail-closed guardrail.
        # Read-only; no cloudfront:UpdateFunction/PublishFunction.
        Sid      = "ReadCloudFrontFunctionOnly"
        Effect   = "Allow"
        Action   = ["cloudfront:GetFunction", "cloudfront:DescribeFunction"]
        Resource = aws_cloudfront_function.v_rewrite.arn
      },
      {
        # aws_cloudfront_distribution.site sets `tags = var.tags`; CloudFront
        # tags are read via a separate tagging API (not embedded in
        # GetDistribution), so refresh needs this too. Resource-scoped to the
        # one distribution.
        Sid      = "ReadDistributionTagsOnly"
        Effect   = "Allow"
        Action   = "cloudfront:ListTagsForResource"
        Resource = aws_cloudfront_distribution.site.arn
      },
      {
        # Read-only: no acm:RequestCertificate, no acm:DeleteCertificate.
        # `aws_acm_certificate.site` sets `tags = var.tags`, and ACM tags are
        # likewise read via a separate call (ListTagsForCertificate), not
        # embedded in DescribeCertificate.
        Sid      = "ReadCertificateOnly"
        Effect   = "Allow"
        Action   = ["acm:DescribeCertificate", "acm:ListTagsForCertificate"]
        Resource = aws_acm_certificate.site.arn
      },

      # ------------------------------------------------------------------
      # READ ONLY — this role's own IAM resources (itself), and the shared
      # OIDC provider `data` source it's looked up through by URL.
      #
      # Not a privilege-escalation surface: none of this grants any write
      # action on IAM. A role reading its own role/policy metadata cannot
      # change its own permissions, and CloudFront/ACM's equivalent
      # already-tracked-by-ARN resources don't need this pattern — the OIDC
      # provider is looked up by `url`, not ARN, which is the one `data`
      # source in this whole module that has no ID to Get-by-ARN with
      # directly; it must List first, then Get the match. Confirmed via the
      # same TF_LOG=debug cross-check as the S3 fixes above: unlike
      # cloudfront:ListDistributions / acm:ListCertificates (removed — never
      # actually called, since those resources ARE tracked by ID in state),
      # iam:ListOpenIDConnectProviders genuinely is called on every refresh.
      # ------------------------------------------------------------------
      {
        Sid      = "ListOpenIDConnectProvidersForRefresh"
        Effect   = "Allow"
        Action   = "iam:ListOpenIDConnectProviders"
        Resource = "*" # No resource-level permission support for this action.
      },
      {
        Sid      = "ReadOpenIDConnectProviderOnly"
        Effect   = "Allow"
        Action   = "iam:GetOpenIDConnectProvider"
        Resource = data.aws_iam_openid_connect_provider.github.arn
      },
      {
        Sid      = "ReadOwnRoleAndPolicyOnly"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"]
        Resource = aws_iam_role.landing_release.arn
      },
    ]
  })
}
