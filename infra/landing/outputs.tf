# ACM validation records — add these as CNAMEs at GoDaddy to validate the cert.
# GoDaddy appends the domain automatically, so enter the HOST part only (strip
# the trailing ".vibesense.live." from record_name) and the value WITHOUT its
# trailing dot. Available right after Phase A (`-target=aws_acm_certificate.site`).
output "acm_validation" {
  description = "CNAME records to add at GoDaddy to validate the certificate."
  value = [for o in aws_acm_certificate.site.domain_validation_options : {
    record_name  = o.resource_record_name
    record_type  = o.resource_record_type
    record_value = o.resource_record_value
  }]
}

# Point the `www` CNAME at GoDaddy to this value (available after Phase B).
output "cloudfront_domain_name" {
  description = "CNAME target for the `www` host at GoDaddy."
  value       = try(aws_cloudfront_distribution.site.domain_name, null)
}

output "site_bucket" {
  value = try(aws_s3_bucket.site.bucket, null)
}

# Not in the original implementation plan's output list, but required by its
# own workflow design: landing-deploy.yml invalidates the cache by reading
# the distribution id from `terraform output` (deliberately not a hardcoded
# ID in the workflow, unlike vibes_bot's admin-console job) — that only works
# if this output exists. Same name/shape as infra/admin/outputs.tf's
# `cloudfront_distribution_id`.
output "cloudfront_distribution_id" {
  description = "Pass to `aws cloudfront create-invalidation` after each deploy."
  value       = try(aws_cloudfront_distribution.site.id, null)
}

# Set this as the `LANDING_RELEASE_ROLE_ARN` GitHub Actions variable (`gh
# variable set`, not a manual UI paste) on marlonreghert/cs-server so
# .github/workflows/landing-deploy.yml can assume it. Not sensitive on its
# own — the OIDC trust condition, not secrecy of the ARN, is what gates
# assumption.
output "landing_release_role_arn" {
  description = "Role ARN for the GitHub Actions landing-site release job."
  value       = aws_iam_role.landing_release.arn
}
