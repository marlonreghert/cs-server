output "site_bucket" {
  description = "S3 bucket the vibes_bot deploy step (`aws s3 sync`) publishes the React build to."
  value       = aws_s3_bucket.site.bucket
}

output "cloudfront_distribution_id" {
  description = "Pass to `aws cloudfront create-invalidation` after each deploy."
  value       = aws_cloudfront_distribution.site.id
}

output "cloudfront_domain_name" {
  description = "Verify the stack directly (before the manual DNS cutover) with `curl -sSI https://<this>/`."
  value       = aws_cloudfront_distribution.site.domain_name
}

output "cloudfront_hosted_zone_id" {
  description = "Fixed CloudFront alias-target hosted zone ID, needed for the manual Route53 ALIAS cutover record (see README.md)."
  value       = aws_cloudfront_distribution.site.hosted_zone_id
}

output "origin_hostname" {
  description = "The new hostname Caddy must serve admin:9000 on (vibes_bot Caddyfile change)."
  value       = var.origin_hostname
}

output "route53_zone_id" {
  description = "Zone the manual public-hostname cutover record goes in."
  value       = data.aws_route53_zone.apex.zone_id
}
