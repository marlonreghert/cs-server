output "bucket_name" {
  description = "Data lake bucket — set this as DATALAKE_BUCKET on cs-server."
  value       = aws_s3_bucket.lake.id
}

output "bucket_arn" {
  value = aws_s3_bucket.lake.arn
}

output "raw_prefix_uri" {
  description = "Point a Glue crawler or Athena table at this."
  value       = "s3://${aws_s3_bucket.lake.id}/raw/"
}

output "athena_results_uri" {
  description = "Athena workgroup query-result location (30-day expiry)."
  value       = "s3://${aws_s3_bucket.lake.id}/_athena_results/"
}

output "writer_policy_arn" {
  value = aws_iam_policy.datalake_writer.arn
}

output "analytics_policy_arn" {
  value = aws_iam_policy.datalake_analytics.arn
}

output "instance_profile_name" {
  description = "Created only when existing_instance_role_name is empty; attach it to the cs-server EC2 (see README)."
  value       = local.create_role ? aws_iam_instance_profile.datalake_writer[0].name : null
}
