variable "name" {
  type = string
}

variable "lambda_zip" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "lambda_security_group_id" {
  type = string
}

variable "connection_env" {
  description = "DB_HOST/DB_PORT/DB_NAME from the database module."
  type        = map(string)
}

variable "dbuser_arn_prefix" {
  type = string
}

variable "token_parameter_name" {
  description = "SSM parameter holding the Socrata app token (created by bootstrap)."
  type        = string
  default     = "/afs/socrata-app-token"
}

variable "since_days" {
  description = "Only fetch inspections from the last N days (dev: 90). null = full dataset."
  type        = number
  default     = null
}

variable "force_destroy" {
  description = "Allow destroying the raw bucket while it still has objects (dev only)."
  type        = bool
  default     = false
}

variable "schedule_enabled" {
  type    = bool
  default = false
}

variable "schedule_expression" {
  description = "In America/Chicago time. Default: Wednesdays 07:00, the day after the city's Tuesday publishes."
  type        = string
  default     = "cron(0 7 ? * WED *)"
}
