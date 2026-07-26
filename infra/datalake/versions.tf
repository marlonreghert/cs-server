terraform {
  required_version = ">= 1.11" # S3 backend native locking (use_lockfile)
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Auth uses AWS IAM Identity Center (SSO). Run `aws sso login --profile <p>`
# first, then `terraform apply` with this provider profile. No long-lived keys.
provider "aws" {
  region  = var.region
  profile = var.aws_profile
}
