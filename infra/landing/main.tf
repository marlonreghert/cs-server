# VibeSense institutional landing site.
#
# Architecture: private S3 origin bucket  ->  CloudFront (OAC, HTTPS)  ->  ACM
# cert (us-east-1). DNS stays authoritative at GoDaddy; the certificate is
# validated with CNAME records added THERE by hand (see outputs), and the site
# is reached via a `www` CNAME -> the CloudFront domain. Apex redirects to www
# via GoDaddy domain forwarding.
#
# APPLY IS TWO-PHASE because DNS lives outside Terraform (GoDaddy):
#   Phase A:  terraform apply -target=aws_acm_certificate.site
#             terraform output acm_validation      # add these CNAMEs at GoDaddy
#             ...wait until the cert is ISSUED...
#   Phase B:  terraform apply                      # validation + CloudFront + policy
# See README.md for the full runbook.

data "aws_caller_identity" "current" {}

locals {
  bucket_name = "vibesense-landing-${data.aws_caller_identity.current.account_id}"
  aliases     = [var.subdomain, var.domain]
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

# Static content. `for_each` over the site/ dir so new files publish on apply.
resource "aws_s3_object" "site" {
  for_each = fileset("${path.module}/site", "**")

  bucket       = aws_s3_bucket.site.id
  key          = each.value
  source       = "${path.module}/site/${each.value}"
  etag         = filemd5("${path.module}/site/${each.value}")
  content_type = lookup(local.content_types, regex("[^.]*$", each.value), "application/octet-stream")
}

# App-association files, served from /.well-known/. Kept OUT of the site/ dir
# (so the fileset glob above never fights over them) and declared explicitly
# because both need `application/json` — and apple-app-site-association MUST be
# extensionless (Apple's requirement), so the extension-based content_type
# lookup can't classify it. Deep links depend on these being reachable at the
# canonical host (www.vibesense.live) over HTTPS with NO redirect — which is
# why the smart link targets www and not the apex (apex 301s to www, and iOS
# does not follow redirects when fetching the AASA).
resource "aws_s3_object" "apple_app_site_association" {
  bucket       = aws_s3_bucket.site.id
  key          = ".well-known/apple-app-site-association"
  source       = "${path.module}/well-known/apple-app-site-association"
  etag         = filemd5("${path.module}/well-known/apple-app-site-association")
  content_type = "application/json"
}

resource "aws_s3_object" "assetlinks" {
  bucket       = aws_s3_bucket.site.id
  key          = ".well-known/assetlinks.json"
  source       = "${path.module}/well-known/assetlinks.json"
  etag         = filemd5("${path.module}/well-known/assetlinks.json")
  content_type = "application/json"
}

locals {
  content_types = {
    html = "text/html; charset=utf-8"
    css  = "text/css; charset=utf-8"
    js   = "application/javascript; charset=utf-8"
    svg  = "image/svg+xml"
    png  = "image/png"
    jpg  = "image/jpeg"
    jpeg = "image/jpeg"
    ico  = "image/x-icon"
    webp = "image/webp"
    txt  = "text/plain; charset=utf-8"
    json = "application/json"
  }
}

# ----------------------------------------------------------------------------
# TLS certificate (us-east-1, required by CloudFront). DNS-validated via GoDaddy.
# ----------------------------------------------------------------------------
resource "aws_acm_certificate" "site" {
  domain_name               = var.subdomain
  subject_alternative_names = [var.domain]
  validation_method         = "DNS"
  tags                      = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

# No Route53 records to point at (DNS is at GoDaddy), so this simply WAITS until
# ACM reports the cert ISSUED after the validation CNAMEs are added by hand.
resource "aws_acm_certificate_validation" "site" {
  certificate_arn = aws_acm_certificate.site.arn
}

# ----------------------------------------------------------------------------
# CloudFront
# ----------------------------------------------------------------------------
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "vibesense-landing-oac"
  description                       = "OAC for the VibeSense landing bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Venue smart links are /v/<id> for any id, but S3 holds a single page. This
# viewer-request function rewrites every /v and /v/<id> to /v.html so one
# object serves them all; the browser URL is unchanged, so v.html's JS still
# reads the id from the path. Scoped to the /v/ prefix (and bare /v) so
# /.well-known/*, /, and everything else pass through untouched.
resource "aws_cloudfront_function" "v_rewrite" {
  name    = "vibesense-v-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrite /v/<id> to /v.html for venue smart links"
  publish = true
  code    = <<-JS
    function handler(event) {
      var req = event.request;
      if (req.uri === '/v' || req.uri.indexOf('/v/') === 0) {
        req.uri = '/v.html';
      }
      return req;
    }
  JS
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "VibeSense landing site"
  default_root_object = "index.html"
  aliases             = local.aliases
  price_class         = "PriceClass_All" # include South America edge locations
  tags                = var.tags

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-landing"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-landing"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.v_rewrite.arn
    }
  }

  # SPA-ish niceties for a single-page site.
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
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
