# Bootstrap for the afs-dev account. Applied by hand with your SSO profile:
#   terraform -chdir=infra/bootstrap/dev init
#   terraform -chdir=infra/bootstrap/dev apply

terraform {
  required_version = ">= 1.10"
  # State lives in the bucket this stack creates. First apply ran with local
  # state; `terraform init -migrate-state` then moved it here.
  backend "s3" {
    bucket       = "afs-tfstate-060516714585"
    key          = "bootstrap/terraform.tfstate"
    region       = "us-east-1"
    profile      = "afs-dev"
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "afs-dev"

  # Refuse to run against any other account, even if the profile is misconfigured.
  allowed_account_ids = ["060516714585"]

  default_tags {
    tags = {
      Project   = "afs"
      Env       = "dev"
      Stack     = "bootstrap"
      ManagedBy = "terraform"
    }
  }
}

module "bootstrap" {
  source = "../../modules/bootstrap"

  env              = "dev"
  github_repo      = "jtravisp/austinfoodscores"
  github_owner_id  = "109884588"
  github_repo_id   = "1411096114"
  apply_subject    = "ref:refs/heads/main"
  budget_name      = "afs-dev-monthly"
  budget_limit_usd = "10.0"
  budget_email     = var.budget_email
}

# The budget was created with the AWS CLI before Terraform existed here.
# This adopts it into state instead of creating a duplicate. After the first
# apply it's a no-op and could be deleted, but it documents where the resource came from.
import {
  to = module.bootstrap.aws_budgets_budget.monthly
  id = "060516714585:afs-dev-monthly"
}

variable "budget_email" {
  description = "Set in budget.local.auto.tfvars (gitignored)."
  type        = string
}

output "state_bucket" {
  value = module.bootstrap.state_bucket
}

output "plan_role_arn" {
  value = module.bootstrap.plan_role_arn
}

output "apply_role_arn" {
  value = module.bootstrap.apply_role_arn
}
