# Remote state — separate KEY from the RDS stack so a landing apply can never
# plan a change to the database. Same bucket (see ../backend-bootstrap).
terraform {
  backend "s3" {
    bucket       = "vibesense-tfstate-839287955684"
    key          = "landing/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
    profile      = "vibesense"
  }
}
