# Remote state — separate KEY from the RDS stack so a landing apply can never
# plan a change to the database. Same bucket (see ../backend-bootstrap).
#
# No `profile` here on purpose: backend blocks cannot reference variables (they
# are evaluated before any Terraform expression is), so a literal profile name
# would be hardcoded for every caller. This backend authenticates through the
# standard AWS credential chain instead — locally, export AWS_PROFILE=vibesense
# before running terraform (see README.md's Prerequisites); in CI,
# aws-actions/configure-aws-credentials already exports explicit
# AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN env vars from the assumed OIDC role,
# which the credential chain picks up with no profile involved at all. A
# hardcoded `profile = "vibesense"` here previously broke every CI run at
# `terraform init` with "failed to get shared config profile, vibesense" — the
# runner has no such local profile to find.
terraform {
  backend "s3" {
    bucket       = "vibesense-tfstate-839287955684"
    key          = "landing/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
