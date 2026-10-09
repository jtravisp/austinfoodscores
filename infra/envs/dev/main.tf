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
}

module "network" {
  source = "../../modules/network"

  name              = local.name
  cidr              = "10.20.0.0/16"
  flow_logs_enabled = false
}
