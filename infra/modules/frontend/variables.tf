variable "name" {
  type = string
}

variable "api_endpoint" {
  description = "API Gateway base URL (https://xxxx.execute-api.<region>.amazonaws.com); becomes the /api/* origin."
  type        = string
}

variable "domain" {
  description = "Custom domain with a Route 53 zone of the same name (prod). null = use the *.cloudfront.net domain (dev)."
  type        = string
  default     = null
}

variable "force_destroy" {
  description = "Allow destroying the site bucket with objects in it (dev)."
  type        = bool
  default     = false
}
