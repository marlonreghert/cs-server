# Remote state — separate KEY from the RDS and landing stacks so a data lake
# apply can never plan a change to the database or the public site. Same bucket
# (see ../backend-bootstrap).
#
# Backend blocks cannot use variables — values are hardcoded on purpose.
terraform {
  backend "s3" {
    bucket       = "vibesense-tfstate-839287955684"
    key          = "datalake/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # native S3 locking (Terraform >= 1.11); no DynamoDB table
    profile      = "vibesense"
  }
}
