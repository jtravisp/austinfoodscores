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
  type = map(string)
}

variable "dbuser_arn_prefix" {
  type = string
}

variable "throttle_rate" {
  description = "Steady-state requests per second across the API."
  type        = number
  default     = 20
}

variable "throttle_burst" {
  type    = number
  default = 40
}
