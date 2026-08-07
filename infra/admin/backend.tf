# Remote state — separate KEY from the landing and RDS stacks so an admin
# apply can never plan a change to either. Same bucket (see ../backend-bootstrap).
terraform {
  backend "s3" {
    bucket       = "vibesense-tfstate-839287955684"
    key          = "admin/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
    profile      = "vibesense"
  }
}
