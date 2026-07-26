# VibeSense raw-response data lake.
#
# One private, versioned, encrypted, single-region S3 bucket holding every
# response we get from BestTime, as Hive-partitioned gzipped NDJSON:
#
#   raw/source=<source>/dataset=<dataset>/dt=<YYYY-MM-DD>/hour=<HH>/part-*.ndjson.gz
#
# `key=value` directories mean Athena / Glue / Spark / Trino / DuckDB discover
# partitions with zero configuration. `curated/` is reserved for a later Parquet
# compaction layer; `_athena_results/` is query scratch with a short expiry.
#
# Cost note: the bill here is dominated by REQUEST count, not storage — which is
# why the writer batches a partition window into one object (~100-300/day)
# instead of one object per API response (~50k/day). See app/dao/datalake_writer.py.

data "aws_caller_identity" "current" {}

locals {
  bucket_name = "vibesense-datalake-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "lake" {
  bucket = local.bucket_name
  tags   = var.tags
}

# Nothing in this bucket is ever public. The only writer is cs-server, and it
# holds PutObject on raw/* and nothing else (see iam.tf).
resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning costs ~nothing on an append-only bucket with unique keys (there are
# no overwrites to keep versions of), and the data is irreplaceable: a live
# busyness reading for a past hour cannot be re-fetched from BestTime.
resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 rather than KMS: KMS bills per request, and this bucket is defined by
# its request count. bucket_key_enabled is a no-op for AES256 but harmless.
resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Housekeeping only — NO storage-class transitions by default, so every object
# stays instantly queryable in S3 Standard forever.
resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket     = aws_s3_bucket.lake.id
  depends_on = [aws_s3_bucket_versioning.lake]

  # Abandoned multipart uploads are invisible in the console but still billed.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Versioning is delete-protection, not history: old versions age out.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # Athena writes one result set per query; they are disposable.
  rule {
    id     = "expire-athena-results"
    status = "Enabled"
    filter {
      prefix = "_athena_results/"
    }
    expiration {
      days = 30
    }
  }

  # Off by default (see variables.tf). Only created when explicitly opted into.
  dynamic "rule" {
    for_each = var.onezone_ia_transition_days > 0 ? [1] : []
    content {
      id     = "raw-to-onezone-ia"
      status = "Enabled"
      filter {
        prefix = "raw/"
      }
      transition {
        days          = var.onezone_ia_transition_days
        storage_class = "ONEZONE_IA"
      }
    }
  }
}

# Refuse plaintext HTTP outright rather than relying on every client to opt in.
resource "aws_s3_bucket_policy" "lake" {
  bucket = aws_s3_bucket.lake.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.lake.arn,
        "${aws_s3_bucket.lake.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

# Free gateway endpoint: archival traffic never leaves the VPC, and NAT
# data-processing charges disappear for S3.
resource "aws_vpc_endpoint" "s3" {
  count             = var.vpc_id == "" ? 0 : 1
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.route_table_ids
  tags              = var.tags
}
