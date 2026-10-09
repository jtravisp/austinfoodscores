# Helper used by the database, ingest, and api modules: one Lambda function
# with its own IAM role and log group. Every function ships the same zip
# (build/lambda.zip); `handler` picks the entry point.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "name" {
  type = string
}

variable "handler" {
  description = "e.g. afs.handlers.migrate.handler"
  type        = string
}

variable "zip_path" {
  type = string
}

variable "timeout" {
  type    = number
  default = 30
}

variable "memory_size" {
  type    = number
  default = 256
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "vpc" {
  description = "Run inside a VPC (null = outside)."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

variable "policy_json" {
  description = "Extra permissions for the function's role (IAM policy JSON), or null."
  type        = string
  default     = null
}

variable "log_retention_days" {
  type    = number
  default = 14
}

# Created explicitly (instead of letting Lambda create it on first run) so
# retention is set and `terraform destroy` removes it.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = var.name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# In a VPC, Lambda must also create/delete network interfaces in your subnets;
# the VPC variant of the managed policy adds those EC2 permissions to logging.
resource "aws_iam_role_policy_attachment" "base" {
  role = aws_iam_role.this.name
  policy_arn = (var.vpc == null
    ? "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  : "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole")
}

resource "aws_iam_role_policy" "extra" {
  count = var.policy_json == null ? 0 : 1

  name   = "app"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  role          = aws_iam_role.this.arn
  handler       = var.handler
  runtime       = "python3.12"
  architectures = ["arm64"]
  filename      = var.zip_path
  # Redeploy only when the zip's contents change (the build is deterministic).
  source_code_hash = filebase64sha256(var.zip_path)
  timeout          = var.timeout
  memory_size      = var.memory_size

  environment {
    variables = var.environment
  }

  dynamic "vpc_config" {
    for_each = var.vpc == null ? [] : [var.vpc]
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy_attachment.base]
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "arn" {
  value = aws_lambda_function.this.arn
}

output "invoke_arn" {
  value = aws_lambda_function.this.invoke_arn
}

output "role_name" {
  value = aws_iam_role.this.name
}
