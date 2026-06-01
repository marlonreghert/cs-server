# RDS Postgres — system of record for venue pipelines.
# Private (no public endpoint); reachable from the EC2 (SG-to-SG) and from
# engineers via SSM port-forward through the EC2 (DBeaver -> localhost). See README.

# Master password generated and stored in Secrets Manager (never in code/state output).
resource "random_password" "master" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "this" {
  name       = "vibesense-rds"
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

# Only the cs-server EC2 security group may reach Postgres. No CIDR ingress.
resource "aws_security_group" "rds" {
  name        = "vibesense-rds"
  description = "RDS Postgres - ingress from cs-server EC2 SG only"
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_security_group_rule" "rds_ingress_from_ec2" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = var.ec2_security_group_id
  description              = "Postgres from cs-server EC2"
}

# Force SSL/TLS in transit.
resource "aws_db_parameter_group" "this" {
  name   = "vibesense-pg16"
  family = "postgres${var.postgres_version}"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  tags = var.tags
}

resource "aws_db_instance" "this" {
  identifier     = "vibesense"
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.instance_class

  db_name  = var.db_name
  username = var.db_username
  password = random_password.master.result

  allocated_storage    = var.allocated_storage_gb
  storage_type         = "gp3"
  storage_encrypted    = true # KMS encryption at rest
  multi_az             = var.multi_az
  publicly_accessible  = false # no public endpoint — access via EC2 / SSM only
  db_subnet_group_name = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name = aws_db_parameter_group.this.name

  backup_retention_period   = 7
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "vibesense-final"
  apply_immediately         = false

  tags = var.tags
}

# Connection details for the app (EC2 injects these as env) and for DBeaver.
resource "aws_secretsmanager_secret" "db" {
  name = "vibesense/rds/credentials"
  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    dbname   = var.db_name
    username = var.db_username
    password = random_password.master.result
    sslmode  = "require"
  })
}
