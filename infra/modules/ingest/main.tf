# Ingest pipeline:
#   [EventBridge Scheduler, prod] -> fetch Lambda (outside VPC) -> S3 raw/ -> S3 event -> load Lambda (in VPC) -> RDS

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  # Built from the name rather than read with a data source: the
  # aws_ssm_parameter data source would copy the decrypted token into state.
  token_parameter_arn = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.token_parameter_name}"
}

# --- Raw snapshot bucket -------------------------------------------------------------------

resource "aws_s3_bucket" "raw" {
  bucket        = "${var.name}-raw-${local.account_id}"
  force_destroy = var.force_destroy # dev: let `terraform destroy` empty it
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "raw" {
  bucket = aws_s3_bucket.raw.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# --- fetch Lambda (outside the VPC: needs the internet, not the database) ----------------------

module "fetch" {
  source = "../lambda_function"

  name        = "${var.name}-fetch"
  handler     = "afs.handlers.fetch.handler"
  zip_path    = var.lambda_zip
  timeout     = 120
  memory_size = 512

  environment = merge(
    {
      RAW_BUCKET  = aws_s3_bucket.raw.bucket
      TOKEN_PARAM = var.token_parameter_name
    },
    var.since_days == null ? {} : { SINCE_DAYS = tostring(var.since_days) },
  )

  # Read one parameter; write only under raw/. (Decrypting with the AWS-managed
  # aws/ssm key needs no KMS statement: its key policy allows use via SSM.)
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = local.token_parameter_arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.raw.arn}/raw/*"
      },
    ]
  })
}

# --- load Lambda (inside the VPC: reaches RDS directly and S3 via the gateway endpoint) -----------

module "load" {
  source = "../lambda_function"

  name        = "${var.name}-load"
  handler     = "afs.handlers.load.handler"
  zip_path    = var.lambda_zip
  timeout     = 120
  memory_size = 512

  environment = merge(var.connection_env, { DB_USER = "afs_loader" })

  vpc = {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.lambda_security_group_id]
  }

  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.raw.arn}/raw/*"
      },
      {
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "${var.dbuser_arn_prefix}/afs_loader"
      },
    ]
  })
}

# Resource-based policy on the function: lets S3 (this bucket, this account) invoke it.
resource "aws_lambda_permission" "s3_invoke_load" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = module.load.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.raw.arn
  source_account = local.account_id
}

resource "aws_s3_bucket_notification" "raw" {
  bucket = aws_s3_bucket.raw.id

  lambda_function {
    lambda_function_arn = module.load.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
    filter_suffix       = ".json.gz"
  }

  # S3 test-invokes the function when the notification is saved; the permission must exist first.
  depends_on = [aws_lambda_permission.s3_invoke_load]
}

# --- Weekly schedule (prod) --------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  count = var.schedule_enabled ? 1 : 0

  name               = "${var.name}-fetch-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  count = var.schedule_enabled ? 1 : 0

  name = "invoke-fetch"
  role = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = module.fetch.arn
    }]
  })
}

resource "aws_scheduler_schedule" "fetch" {
  count = var.schedule_enabled ? 1 : 0

  name                         = "${var.name}-weekly-fetch"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "America/Chicago"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = module.fetch.arn
    role_arn = aws_iam_role.scheduler[0].arn
  }
}
