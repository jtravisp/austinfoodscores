# RDS Postgres (private, IAM auth) plus the migrate Lambda that manages its schema.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  db_name     = "afs"
  master_user = "afs_admin"
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name   = var.name
  family = "postgres17"

  # Reject any non-TLS connection (IAM auth needs TLS anyway).
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Log statements slower than 1s, for spotting missing indexes.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "17" # major only: AWS picks the current minor; auto_minor_version_upgrade keeps it patched
  instance_class = var.instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = local.db_name
  username = local.master_user
  # RDS generates the master password and keeps it in Secrets Manager (with
  # automatic rotation). It never appears in Terraform config or state.
  manage_master_user_password         = true
  iam_database_authentication_enabled = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.rds_security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period    = var.backup_retention_days
  deletion_protection        = var.deletion_protection
  skip_final_snapshot        = var.skip_final_snapshot
  final_snapshot_identifier  = var.skip_final_snapshot ? null : "${var.name}-final"
  auto_minor_version_upgrade = true
  apply_immediately          = var.apply_immediately
}

locals {
  # IAM resource for `rds-db:connect`, per database user.
  dbuser_arn_prefix = "arn:aws:rds-db:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:dbuser:${aws_db_instance.this.resource_id}"

  connection_env = {
    DB_HOST = aws_db_instance.this.address
    DB_PORT = tostring(aws_db_instance.this.port)
    DB_NAME = local.db_name
  }
}

# --- migrate Lambda ---------------------------------------------------------------------

module "migrate" {
  source = "../lambda_function"

  name     = "${var.name}-migrate"
  handler  = "afs.handlers.migrate.handler"
  zip_path = var.lambda_zip
  timeout  = 120

  environment = merge(local.connection_env, {
    DB_USER        = "afs_migrator"
    DB_MASTER_USER = local.master_user
  })

  vpc = {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.lambda_security_group_id]
  }

  # May log in as afs_migrator with an IAM token. Note it has NO access to the
  # master secret: the password arrives in the invocation payload.
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "${local.dbuser_arn_prefix}/afs_migrator"
    }]
  })
}
