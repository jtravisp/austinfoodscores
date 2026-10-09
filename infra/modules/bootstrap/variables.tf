variable "env" {
  description = "Environment name (dev or prod); used in resource names."
  type        = string
}

variable "github_repo" {
  description = "owner/name of the GitHub repo allowed to assume the CI roles."
  type        = string
}

variable "apply_subject" {
  description = <<-EOT
    The OIDC `sub` claim suffix allowed to assume the apply role, e.g.
    "ref:refs/heads/main" (dev: any run on main) or
    "environment:production" (prod: only jobs in the protected GitHub environment).
  EOT
  type        = string
}

variable "budget_name" {
  type = string
}

variable "budget_limit_usd" {
  description = "Monthly cost budget, as AWS stores it (e.g. \"10.0\")."
  type        = string
}

variable "budget_email" {
  description = "Where budget alerts go. Set in a gitignored *.local.auto.tfvars file."
  type        = string
}
