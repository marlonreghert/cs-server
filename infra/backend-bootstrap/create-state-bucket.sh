#!/usr/bin/env bash
# Create the S3 bucket that stores ALL Terraform state for this account
# (rds/, landing/, and any future stack).
#
# This bucket cannot store its own state (chicken-and-egg), so it is created
# here with the AWS CLI rather than Terraform. Safe to re-run: the create call
# is tolerated if the bucket already exists, and every put-* call is idempotent.
#
# VERSIONING is the key protection — it is why we will never lose state again.
set -euo pipefail

PROFILE="${AWS_PROFILE:-vibesense}"
REGION="us-east-1"

ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
BUCKET="vibesense-tfstate-${ACCOUNT}"

echo ">> State bucket: ${BUCKET}  (account ${ACCOUNT}, region ${REGION})"

# us-east-1 must NOT be given a LocationConstraint.
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --profile "$PROFILE" \
  || echo "   (create-bucket returned non-zero — likely already exists; continuing)"

echo ">> Enabling versioning"
aws s3api put-bucket-versioning --bucket "$BUCKET" --profile "$PROFILE" \
  --versioning-configuration Status=Enabled

echo ">> Enabling default encryption (SSE-S3 / AES256)"
aws s3api put-bucket-encryption --bucket "$BUCKET" --profile "$PROFILE" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

echo ">> Blocking all public access"
aws s3api put-public-access-block --bucket "$BUCKET" --profile "$PROFILE" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo ">> Applying TLS-only bucket policy"
aws s3api put-bucket-policy --bucket "$BUCKET" --profile "$PROFILE" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"DenyInsecureTransport\",
    \"Effect\": \"Deny\",
    \"Principal\": \"*\",
    \"Action\": \"s3:*\",
    \"Resource\": [\"arn:aws:s3:::${BUCKET}\", \"arn:aws:s3:::${BUCKET}/*\"],
    \"Condition\": { \"Bool\": { \"aws:SecureTransport\": \"false\" } }
  }]
}"

echo ">> Done. Backend config uses: bucket=${BUCKET}, region=${REGION}."
echo "   Next: cd ../rds && terraform init -migrate-state"
