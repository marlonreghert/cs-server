# Credentials for the data lake — an EC2 instance ROLE, never an access key.
#
# WHY: an IAM user means a long-lived secret that lands in Terraform state, has
# to be injected into the container environment, and never rotates. An instance
# role gives boto3 short-lived, auto-rotating credentials from IMDSv2, and there
# is no secret to leak — not in state, not in .env, not in CI.

# ── Writer: append-only, raw/ only ───────────────────────────────────────────
# No GetObject, no DeleteObject, no ListBucket. A fully compromised cs-server
# can add records to the lake and can neither read nor destroy what is there.
resource "aws_iam_policy" "datalake_writer" {
  name        = "vibesense-datalake-writer"
  description = "Append-only access to the VibeSense data lake raw/ prefix"
  tags        = var.tags

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AppendRawObjects"
      Effect   = "Allow"
      Action   = ["s3:PutObject"]
      Resource = "${aws_s3_bucket.lake.arn}/raw/*"
    }]
  })
}

# ── Analytics: read the lake, write only query scratch ───────────────────────
# For humans and Athena. Attach to whatever principal runs the queries.
resource "aws_iam_policy" "datalake_analytics" {
  name        = "vibesense-datalake-analytics"
  description = "Read the VibeSense data lake and write Athena query results"
  tags        = var.tags

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadLake"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.lake.arn}/raw/*",
          "${aws_s3_bucket.lake.arn}/curated/*",
        ]
      },
      {
        Sid      = "ListLake"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.lake.arn
      },
      {
        Sid      = "WriteQueryResults"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.lake.arn}/_athena_results/*"
      },
    ]
  })
}

# ── Attach the writer policy ─────────────────────────────────────────────────
# Two shapes, because the EC2 predates Terraform and can hold only one instance
# profile. See variables.tf.

locals {
  create_role = var.existing_instance_role_name == ""
}

# (a) The instance already has a role: just attach.
resource "aws_iam_role_policy_attachment" "existing_role_writer" {
  count      = local.create_role ? 0 : 1
  role       = var.existing_instance_role_name
  policy_arn = aws_iam_policy.datalake_writer.arn
}

# (b) No role yet: create one + its instance profile. Associating it with the
# running instance is a one-time manual step (see README).
resource "aws_iam_role" "datalake_writer" {
  count = local.create_role ? 1 : 0
  name  = "vibesense-cs-server"
  tags  = var.tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "new_role_writer" {
  count      = local.create_role ? 1 : 0
  role       = aws_iam_role.datalake_writer[0].name
  policy_arn = aws_iam_policy.datalake_writer.arn
}

resource "aws_iam_instance_profile" "datalake_writer" {
  count = local.create_role ? 1 : 0
  name  = "vibesense-cs-server"
  role  = aws_iam_role.datalake_writer[0].name
  tags  = var.tags
}
