# Remote state — its OWN key, separate from datalake/, admin/, landing/ and
# rds/. This is not bookkeeping: a shared key would let a `media` plan/apply
# propose changes to `infra/datalake`, and the datalake writer policy's
# `description` is IMMUTABLE in AWS. Editing it forces a destroy-and-recreate
# of the policy and its role attachment, opening a window in which cs-server
# holds no PutObject — and a data-lake flush in that window is DROPPED, not
# retried, losing BestTime observations that cannot be re-fetched.
#
# `infra/datalake` also has known drift (a bare apply plans
# "3 to add, 1 to change, 2 to destroy", including destroying the S3 VPC
# gateway endpoint). Separate state is what makes it structurally impossible
# for this feature to touch any of it.
#
# Backend blocks cannot use variables — values are hardcoded on purpose.
terraform {
  backend "s3" {
    bucket       = "vibesense-tfstate-839287955684"
    key          = "media/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # native S3 locking (Terraform >= 1.11); no DynamoDB table
    profile      = "vibesense"
  }
}
