output "media_bucket" {
  description = "Set this as cs-server's MEDIA_BUCKET."
  value       = aws_s3_bucket.media.bucket
}

output "media_cdn_base_url" {
  description = "Set this as cs-server's MEDIA_CDN_BASE_URL. Every stored photo_url is built from it, so it must not change once rows exist."
  value       = "https://${var.public_hostname}"
}

output "cloudfront_distribution_id" {
  description = "Pass to `aws cloudfront create-invalidation` if an object ever has to be purged. Content-addressed keys mean this should never be routine."
  value       = aws_cloudfront_distribution.media.id
}

output "cloudfront_domain_name" {
  description = "Verify the stack directly, before DNS has propagated: `curl -sSI https://<this>/<key>`."
  value       = aws_cloudfront_distribution.media.domain_name
}

output "media_writer_policy_arn" {
  description = "Attach to the cs-server EC2 instance role if `cs_server_role_name` was left empty. This is a NEW policy — never attach or edit the datalake writer instead."
  value       = aws_iam_policy.media_profile_photo_writer.arn
}

output "route53_zone_id" {
  description = "Zone holding the media A/ALIAS record."
  value       = data.aws_route53_zone.apex.zone_id
}
