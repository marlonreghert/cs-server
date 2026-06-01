variable "region" {
  description = "AWS region — MUST match the cs-server EC2's region (co-locate for low write-through latency)."
  type        = string
}

variable "aws_profile" {
  description = "Local AWS SSO profile name used for `terraform apply`."
  type        = string
}

variable "vpc_id" {
  description = "VPC of the cs-server EC2 (RDS lives in the same VPC)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the RDS subnet group (>= 2 AZs required by AWS)."
  type        = list(string)
}

variable "ec2_security_group_id" {
  description = "Security group of the cs-server EC2 — the only source allowed to reach RDS:5432."
  type        = string
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "vibesense"
}

variable "db_username" {
  description = "Master username."
  type        = string
  default     = "vibesense_admin"
}

variable "instance_class" {
  description = "RDS instance class. Start small; resize later."
  type        = string
  default     = "db.t4g.small"
}

variable "allocated_storage_gb" {
  description = "Initial gp3 storage in GB."
  type        = number
  default     = 20
}

variable "multi_az" {
  description = "Multi-AZ for HA. Off to start (cost); enable later."
  type        = bool
  default     = false
}

variable "postgres_version" {
  type    = string
  default = "16"
}

variable "tags" {
  type    = map(string)
  default = { project = "vibesense", component = "rds-system-of-record" }
}
