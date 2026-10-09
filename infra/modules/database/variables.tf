variable "name" {
  description = "Name prefix and RDS identifier, e.g. afs-dev."
  type        = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "rds_security_group_id" {
  type = string
}

variable "lambda_security_group_id" {
  type = string
}

variable "lambda_zip" {
  type = string
}

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "backup_retention_days" {
  description = "0 disables automated backups (dev); prod keeps 7."
  type        = number
}

variable "deletion_protection" {
  type = bool
}

variable "skip_final_snapshot" {
  type = bool
}

variable "apply_immediately" {
  description = "Apply modifications now instead of in the next maintenance window."
  type        = bool
  default     = false
}
