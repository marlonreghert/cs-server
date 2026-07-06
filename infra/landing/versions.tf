terraform {
  required_version = ">= 1.11" # S3 backend native locking (use_lockfile)
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# CloudFront + ACM for a public HTTPS site MUST live in us-east-1 (CloudFront
# only accepts ACM certificates from us-east-1). S3 and CloudFront are otherwise
# region-agnostic, so one us-east-1 provider covers the whole stack.
provider "aws" {
  region  = "us-east-1"
  profile = var.aws_profile
}
