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
