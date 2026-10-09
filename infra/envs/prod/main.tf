# The prod environment: https://austinfood.travispollard.com
#
# Same modules as dev; the differences are all here and all deliberate:
# full dataset, weekly schedule, protected database, flow logs, custom domain,
# alarms. Applied by CI from the protected `production` GitHub environment.
# Locally:  $env:AWS_PROFILE = "afs-prod"; terraform -chdir=infra/envs/prod plan

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket       = "afs-tfstate-169406897968"
    key          = "env/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["169406897968"]

  default_tags {
    tags = {
      Project   = "afs"
      Env       = "prod"
      ManagedBy = "terraform"
    }
  }
}

variable "alarm_email" {
  description = "Alarm recipient. Locally: alarm.local.auto.tfvars (gitignored). CI: the ALARM_EMAIL Actions variable."
  type        = string
}

locals {
  name       = "afs-prod"
  lambda_zip = "${path.root}/../../../build/lambda.zip"
}

module "network" {
  source = "../../modules/network"

  name              = local.name
  cidr              = "10.21.0.0/16"
  flow_logs_enabled = true
}

module "database" {
  source = "../../modules/database"

  name                     = local.name
  subnet_ids               = module.network.private_subnet_ids
  rds_security_group_id    = module.network.rds_security_group_id
  lambda_security_group_id = module.network.lambda_security_group_id
  lambda_zip               = local.lambda_zip

  backup_retention_days = 7
  deletion_protection   = true
  skip_final_snapshot   = false
  apply_immediately     = false # changes wait for the maintenance window
}

module "ingest" {
  source = "../../modules/ingest"

  name                     = local.name
  lambda_zip               = local.lambda_zip
  subnet_ids               = module.network.private_subnet_ids
  lambda_security_group_id = module.network.lambda_security_group_id
  connection_env           = module.database.connection_env
  dbuser_arn_prefix        = module.database.dbuser_arn_prefix

  since_days       = null  # full dataset
  force_destroy    = false # the raw archive is the replay history
  schedule_enabled = true  # Wednesdays 07:00 America/Chicago
}

module "api" {
  source = "../../modules/api"

  name                     = local.name
  lambda_zip               = local.lambda_zip
  subnet_ids               = module.network.private_subnet_ids
  lambda_security_group_id = module.network.lambda_security_group_id
  connection_env           = module.database.connection_env
  dbuser_arn_prefix        = module.database.dbuser_arn_prefix
}

module "frontend" {
  source = "../../modules/frontend"

  name         = local.name
  api_endpoint = module.api.endpoint
  domain       = "austinfood.travispollard.com" # zone created by infra/dns
}

module "monitoring" {
  source = "../../modules/monitoring"

  name        = local.name
  alarm_email = var.alarm_email
  function_names = {
    fetch = module.ingest.fetch_function_name
    load  = module.ingest.load_function_name
    query = module.api.query_function_name
  }
  api_id        = module.api.api_id
  db_identifier = module.database.identifier
}

output "site_url" {
  value = module.frontend.url
}

output "site_bucket" {
  value = module.frontend.site_bucket
}

output "distribution_id" {
  value = module.frontend.distribution_id
}

output "api_endpoint" {
  value = module.api.endpoint
}

output "raw_bucket" {
  value = module.ingest.raw_bucket
}
