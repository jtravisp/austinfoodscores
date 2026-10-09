# The dev environment. Locally:  $env:AWS_PROFILE = "afs-dev"  (PowerShell)
#                                export AWS_PROFILE=afs-dev     (bash)
# then: terraform -chdir=infra/envs/dev init / plan / apply
# In CI, credentials come from the GitHub OIDC roles instead of a profile,
# which is why no `profile` is set here.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket       = "afs-tfstate-060516714585"
    key          = "env/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["060516714585"]

  default_tags {
    tags = {
      Project   = "afs"
      Env       = "dev"
      ManagedBy = "terraform"
    }
  }
}

locals {
  name = "afs-dev"
  # Built by `uv run python scripts/build_lambda.py` before plan/apply.
  lambda_zip = "${path.root}/../../../build/lambda.zip"
}

module "network" {
  source = "../../modules/network"

  name              = local.name
  cidr              = "10.20.0.0/16"
  flow_logs_enabled = false
}

# Dev is disposable: no backups, no final snapshot, no deletion protection.
# `terraform destroy` between sessions; rebuild with apply + remote-migrate + load.
module "database" {
  source = "../../modules/database"

  name                     = local.name
  subnet_ids               = module.network.private_subnet_ids
  rds_security_group_id    = module.network.rds_security_group_id
  lambda_security_group_id = module.network.lambda_security_group_id
  lambda_zip               = local.lambda_zip

  backup_retention_days = 0
  deletion_protection   = false
  skip_final_snapshot   = true
  apply_immediately     = true
}

output "db_address" {
  value = module.database.address
}

output "migrate_function_name" {
  value = module.database.migrate_function_name
}
