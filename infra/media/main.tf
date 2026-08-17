# VibeSense app-servable media — private S3 + CloudFront (OAC).
#
# Holds media the APP is allowed to display, starting with venue Instagram
# profile photos (plans/260816_instagram-profile-photo-hero.md):
#
#   venue-profile-photos/<venue_id>/<sha256[:16]>.jpg
#
# ## Why this is a separate stack from infra/datalake
#
# The data lake is internal-use-only by design (docs/venue-retrieval-storage.md
# §8): it blocks public access AND its writer role is deliberately denied
# s3:GetObject, so nothing there can ever be served to a user. That is a
# compliance boundary enforced by infra, not by convention, and it is exactly
# why this feature gets its own bucket, its own policy and its own state key
# rather than widening the lake's.
#
# Two further hard reasons, both recorded in that doc's §5:
#
#   1. `aws_iam_policy.description` is IMMUTABLE in AWS. Editing the datalake
#      writer policy forces a destroy-and-recreate of it and its role
#      attachment, opening a window where cs-server holds no PutObject — and a
#      lake flush in that window is DROPPED, not retried, losing BestTime
#      observations that cannot be re-fetched. So the grant below is a NEW,
#      separately-named policy. It does not extend, rename or re-describe the
#      datalake one.
#   2. `infra/datalake` has known drift (a bare apply plans "3 to add, 1 to
#      change, 2 to destroy", including destroying the S3 VPC gateway
#      endpoint). Separate state makes it structurally impossible for a media
#      apply to touch it.
#
# ## Why this is single-phase
#
# Modelled on infra/admin/, which is merged and proven. apivibesensemiddleware
# .click is Route53-hosted in this account, so this stack creates ACM's own
# validation records and `aws_acm_certificate_validation` waits inline: ONE
# apply builds the cert, the bucket, the DNS record and the distribution.
# (infra/landing/ needed two phases only because vibesense.live's DNS is
# authoritative at GoDaddy.)
#
# ## Apply BEFORE enabling the job
#
# A write outside the IAM policy fails AFTER the Apify scrape has already been
# paid for. `instagram_profile_photo_enabled` therefore ships false and is
# turned on only once this stack is applied and verified — see README.md.

data "aws_caller_identity" "current" {}

data "aws_route53_zone" "apex" {
  name = "${var.apex_domain}."
}

locals {
  bucket_name = "vibesense-media-${data.aws_caller_identity.current.account_id}"
  # Every object this stack serves lives under this prefix, and the write grant
  # below is scoped to exactly it. Widening it is a terraform apply FIRST.
  profile_photo_prefix = "venue-profile-photos"
}

# ----------------------------------------------------------------------------
# Origin bucket — private. Public reach comes from CloudFront, never an ACL.
# ----------------------------------------------------------------------------
resource "aws_s3_bucket" "media" {
  bucket = local.bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning on a content-addressed bucket costs ~nothing (unique keys mean
# there are no overwrites to keep versions of) and turns an accidental delete
# from data loss into a restore.
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 rather than KMS: KMS bills per request, and CloudFront reads every
# object it has not cached. bucket_key_enabled is a no-op for AES256.
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Housekeeping only — NO storage-class transitions. These objects are read by
# the app on every cold list render, so archival tiers would add restore
# latency to a user-facing image for a rounding error in storage cost.
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # A superseded profile photo is a NON-CURRENT version only if a key was
  # overwritten, which content addressing makes rare. Expire those after 30
  # days so an overwrite cannot accumulate cost forever.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# ----------------------------------------------------------------------------
# TLS certificate (us-east-1, required by CloudFront). DNS-validated through
# the Route53 zone this account already controls — fully automated.
# ----------------------------------------------------------------------------
resource "aws_acm_certificate" "media" {
  domain_name       = var.public_hostname
  validation_method = "DNS"
  tags              = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.media.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.apex.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "media" {
  certificate_arn         = aws_acm_certificate.media.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ----------------------------------------------------------------------------
# CloudFront
# ----------------------------------------------------------------------------
resource "aws_cloudfront_origin_access_control" "media" {
  name                              = "vibesense-media-oac"
  description                       = "OAC for the VibeSense app media bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "media" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "VibeSense app media (venue profile photos)"
  aliases         = [var.public_hostname]
  # South America edge locations included deliberately: the whole catalog is in
  # Recife, so PriceClass_100 would serve every user from another continent.
  price_class = "PriceClass_All"
  tags        = var.tags

  # No default_root_object: this distribution serves addressed objects, never
  # an index page. A bare request to / is a 403 from S3, which is correct.

  origin {
    domain_name              = aws_s3_bucket.media.bucket_regional_domain_name
    origin_id                = "s3-media"
    origin_access_control_id = aws_cloudfront_origin_access_control.media.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-media"
    viewer_protocol_policy = "redirect-to-https"
    # Read-only. There is no upload path through CloudFront: cs-server writes
    # to S3 directly with its instance role.
    allowed_methods = ["GET", "HEAD"]
    cached_methods  = ["GET", "HEAD"]
    # Images are already compressed; re-compressing them buys nothing and costs
    # CPU at the edge.
    compress        = false
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.media.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# ----------------------------------------------------------------------------
# Public DNS. Safe to manage here — unlike infra/admin/'s public hostname, this
# record does not exist yet, so there is no unowned production record to adopt.
# ----------------------------------------------------------------------------
resource "aws_route53_record" "media" {
  zone_id = data.aws_route53_zone.apex.zone_id
  name    = var.public_hostname
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.media.domain_name
    zone_id                = aws_cloudfront_distribution.media.hosted_zone_id
    evaluate_target_health = false
  }
}

# ----------------------------------------------------------------------------
# Allow ONLY this CloudFront distribution to read the bucket (OAC).
# ----------------------------------------------------------------------------
resource "aws_s3_bucket_policy" "media" {
  bucket = aws_s3_bucket.media.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.media.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.media.arn
        }
      }
    }]
  })
}

# ----------------------------------------------------------------------------
# cs-server's write grant — a NEW, separately-named policy.
#
# It grants PutObject on `venue-profile-photos/*` in THIS bucket and nothing
# else: no GetObject (cs-server never reads these back — the unchanged-photo
# check compares a hash held in RDS, precisely so no read grant is needed), no
# DeleteObject, no ListBucket, no wildcard resource, and no reach into the data
# lake.
#
# `description` is set once here and must never be edited afterwards for the
# same reason it must never be edited on the datalake policy: the field is
# immutable in AWS, so a change forces destroy-and-recreate of the policy and
# its attachment.
# ----------------------------------------------------------------------------
resource "aws_iam_policy" "media_profile_photo_writer" {
  name        = "vibesense-media-profile-photo-writer"
  description = "Write venue profile photos into the VibeSense app media bucket"
  tags        = var.tags

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PutProfilePhotos"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.media.arn}/${local.profile_photo_prefix}/*"
      },
    ]
  })
}

# Attached only when the role name is supplied. Left unset, the policy still
# exists and an operator attaches it once by hand — the same "the EC2 predates
# Terraform and holds one instance profile" reality infra/datalake works
# around. Attaching a policy does not modify the role or any other policy on
# it, so this cannot disturb the datalake grant.
resource "aws_iam_role_policy_attachment" "cs_server_media_writer" {
  count      = var.cs_server_role_name == "" ? 0 : 1
  role       = var.cs_server_role_name
  policy_arn = aws_iam_policy.media_profile_photo_writer.arn
}
