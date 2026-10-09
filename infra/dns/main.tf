# Delegated zone for austinfood.travispollard.com, owned by the afs-prod account.
#
# Same pattern as ../armybandncoer/infra/dns. One apply here:
#   1. creates the child zone in afs-prod, and
#   2. writes the NS delegation record into the parent travispollard.com zone,
#      which lives in another account (679878703800, CLI profile tp-site).
# After that, afs-prod owns every record under the subdomain (the ACM
# validation records, the CloudFront aliases) and never writes to the parent
# zone again. That autonomy is what the $0.50/month hosted zone buys.
#
# Applied by hand, never by CI: the CI roles have no path into the parent account.
#   aws sso login --sso-session tpollard     (afs-prod)
#   aws sso login --sso-session tp-site      (parent zone)
#   terraform -chdir=infra/dns init && terraform -chdir=infra/dns apply

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
    key          = "dns/terraform.tfstate"
    region       = "us-east-1"
    profile      = "afs-prod"
    use_lockfile = true
  }
}

locals {
  subdomain               = "austinfood.travispollard.com"
  parent_domain           = "travispollard.com"
  expected_parent_account = "679878703800"
}

# Default provider: afs-prod, which owns the child zone.
provider "aws" {
  region              = "us-east-1"
  profile             = "afs-prod"
  allowed_account_ids = ["169406897968"]

  default_tags {
    tags = {
      Project   = "afs"
      Env       = "prod"
      Stack     = "dns"
      ManagedBy = "terraform"
    }
  }
}

# Second provider: the account that owns travispollard.com. Writing the
# delegation from here wires the NS values resource-to-resource, so there is
# no copy-paste step into the other repo and nothing to drift.
provider "aws" {
  alias               = "dns_parent"
  region              = "us-east-1"
  profile             = "tp-site"
  allowed_account_ids = [local.expected_parent_account]
}

# --- child zone (afs-prod) ---------------------------------------------------------

resource "aws_route53_zone" "this" {
  name    = local.subdomain
  comment = "Delegated from ${local.parent_domain} (account ${local.expected_parent_account}). Managed by the austinfoodscores repo, infra/dns."
}

# --- delegation (parent account) ---------------------------------------------------

data "aws_route53_zone" "parent" {
  provider     = aws.dns_parent
  name         = "${local.parent_domain}."
  private_zone = false
}

# The one record this project writes outside its own accounts. The parent
# zone's own Terraform (../travispollard.com, modules/route53) manages specific
# records and has no knowledge of this one, so it won't remove it: Terraform
# only manages what's in its own state. 172800 s (2 days) is Route 53's own TTL
# for NS records; delegations rarely change.
resource "aws_route53_record" "delegation" {
  provider = aws.dns_parent

  zone_id = data.aws_route53_zone.parent.zone_id
  name    = local.subdomain
  type    = "NS"
  ttl     = 172800
  records = aws_route53_zone.this.name_servers
}

output "zone_id" {
  description = "The prod frontend finds the zone by name with a data source; this is for reference."
  value       = aws_route53_zone.this.zone_id
}

output "name_servers" {
  value = aws_route53_zone.this.name_servers
}

output "verify_command" {
  description = "Delegation is live when this returns the name servers above."
  value       = "nslookup -type=NS ${local.subdomain}"
}
