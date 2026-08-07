# VibeSense admin panel — private S3 + CloudFront hosting, same-origin with
# the existing FastAPI admin API.
#
# Architecture: private S3 origin bucket (React build) + a CloudFront custom
# origin (Caddy -> admin:9000, unchanged) behind ONE distribution at
# admin.apivibesensemiddleware.click. Ordered cache behaviors send /login,
# /logout, /health, and /api/* to the API origin; everything else serves the
# React static build from S3. Same public hostname end-to-end means the
# browser sees one origin, so the existing signed session cookie
# (vibes_bot/app/admin/auth.py) needs no CORS/token rewrite.
#
# Unlike infra/landing/ (DNS at GoDaddy, manual CNAME validation),
# apivibesensemiddleware.click is Route53-hosted in this account (confirmed:
# `dig +short apivibesensemiddleware.click NS` -> awsdns-*), so ACM
# validation is fully automated in ONE `terraform apply` — no phased apply
# needed for the certificate.
#
# This stack deliberately does NOT manage the existing
# admin.apivibesensemiddleware.click record (today an A record straight at
# the EC2 host) — Terraform didn't create it and adopting an unowned
# production record risks drift or accidental deletion on a future destroy.
# The final cutover (repointing that record to this distribution) is a
# manual, reviewed step — see README.md.

data "aws_caller_identity" "current" {}

data "aws_route53_zone" "apex" {
  name = "${var.apex_domain}."
}

locals {
  bucket_name = "vibesense-admin-${data.aws_caller_identity.current.account_id}"
}

# ----------------------------------------------------------------------------
# Origin bucket — private; only CloudFront may read it.
# ----------------------------------------------------------------------------
resource "aws_s3_bucket" "site" {
  bucket = local.bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "site" {
  bucket = aws_s3_bucket.site.id
  versioning_configuration {
    status = "Enabled"
  }
}

# No object-upload block here (unlike infra/landing/'s fileset-driven
# aws_s3_object): the admin app redeploys far more often than the landing
# page, so publishing the built React app is a separate step (vibes_bot's
# deploy script: `aws s3 sync` + CloudFront invalidation), not Terraform's
# job. This stack only owns the bucket and its policy.

# ----------------------------------------------------------------------------
# New origin-facing hostname for Caddy. Safe to manage in Terraform — it
# doesn't exist yet, so there's no pre-existing record to adopt.
# ----------------------------------------------------------------------------
resource "aws_route53_record" "origin" {
  zone_id = data.aws_route53_zone.apex.zone_id
  name    = var.origin_hostname
  type    = "A"
  ttl     = 300
  records = [var.origin_ip]
}

# ----------------------------------------------------------------------------
# TLS certificate (us-east-1, required by CloudFront). DNS-validated via the
# Route53 zone this account already controls -- fully automated.
# ----------------------------------------------------------------------------
resource "aws_acm_certificate" "site" {
  domain_name       = var.public_hostname
  validation_method = "DNS"
  tags              = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.site.domain_validation_options : dvo.domain_name => {
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

resource "aws_acm_certificate_validation" "site" {
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ----------------------------------------------------------------------------
# CloudFront
# ----------------------------------------------------------------------------
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "vibesense-admin-oac"
  description                       = "OAC for the VibeSense admin panel bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "VibeSense admin panel"
  default_root_object = "index.html"
  aliases             = [var.public_hostname]
  price_class         = "PriceClass_All" # include South America edge locations
  tags                = var.tags

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-admin-ui"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  origin {
    domain_name = var.origin_hostname
    origin_id   = "admin-api"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Default: everything not matched below serves the React build from S3.
  # No SPA 404->index.html rewrite needed -- the app (and its predecessor)
  # is a single unrouted page; there's nothing else under /* to redirect.
  default_cache_behavior {
    target_origin_id       = "s3-admin-ui"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  }

  # Auth + API traffic goes to the FastAPI origin, uncached, with every
  # header/cookie/query-string forwarded intact -- this is what keeps the
  # existing signed session cookie working unmodified.
  dynamic "ordered_cache_behavior" {
    for_each = ["/login", "/logout", "/health", "/api/*"]
    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = "admin-api"
      viewer_protocol_policy   = "redirect-to-https"
      allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      compress                 = true
      cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
      origin_request_policy_id = "216adef6-5c7f-47e4-b989-5492eafa07d3" # Managed-AllViewer
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# ----------------------------------------------------------------------------
# Allow ONLY this CloudFront distribution to read the bucket (OAC).
# ----------------------------------------------------------------------------
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.site.arn
        }
      }
    }]
  })
}
