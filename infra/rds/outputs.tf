output "rds_endpoint" {
  description = "RDS endpoint host (private)."
  value       = aws_db_instance.this.address
}

output "rds_port" {
  value = aws_db_instance.this.port
}

output "rds_security_group_id" {
  value = aws_security_group.rds.id
}

output "credentials_secret_arn" {
  description = "Secrets Manager ARN holding host/port/db/user/password/sslmode."
  value       = aws_secretsmanager_secret.db.arn
}
