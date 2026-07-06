# Remote state — S3 (versioned + encrypted + native S3 locking).
#
# WHY: the original state was LOCAL-ONLY and gitignored, so moving the repo
# folder risked losing the sole record of the production RDS. Bucket VERSIONING
# is the real "never lose it again" protection (every write keeps prior copies).
#
# Backend blocks cannot use variables — values are hardcoded on purpose.
# Bucket must already exist (see ../backend-bootstrap/). First-time migration of
# the existing local state:  terraform init -migrate-state
terraform {
  backend "s3" {
    bucket       = "vibesense-tfstate-839287955684"
    key          = "rds/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # native S3 locking (Terraform >= 1.11); no DynamoDB table
    profile      = "vibesense"
  }
}
