variable "name" {
  description = "Name prefix, e.g. afs-dev."
  type        = string
}

variable "cidr" {
  description = "VPC CIDR (a /16). Distinct per env so they could be peered later."
  type        = string
}

variable "az_count" {
  description = "Number of AZs (and private subnets). RDS needs at least 2."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2
    error_message = "RDS subnet groups need at least 2 AZs."
  }
}

variable "flow_logs_enabled" {
  description = "Send VPC flow logs to CloudWatch (on in prod)."
  type        = bool
  default     = false
}

variable "flow_log_retention_days" {
  type    = number
  default = 30
}
