terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Auth uses AWS IAM Identity Center (SSO). Run `aws sso login --profile <p>`
# first, then `terraform apply` with this provider profile. No long-lived keys.
provider "aws" {
  region  = var.region
  profile = var.aws_profile
}
