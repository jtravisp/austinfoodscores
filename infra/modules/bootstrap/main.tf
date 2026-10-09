# Per-account foundation, applied once by hand (never by CI):
#   - S3 bucket for Terraform state (this stack's own state included)
#   - GitHub Actions OIDC identity provider
#   - CI roles: read-only "plan" for pull requests, admin "apply" for main/production
#   - The monthly cost budget (created by CLI first, adopted via import)

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  oidc_host  = "token.actions.githubusercontent.com"
}

# --- Terraform state bucket ----------------------------------------------------------

resource "aws_s3_bucket" "state" {
  # Bucket names are global across all AWS accounts; the account ID makes it unique.
  bucket = "afs-tfstate-${local.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

# Versioning is the undo button for state: a bad apply or corrupted state can
# be rolled back to a previous object version.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days           = 90
      newer_noncurrent_versions = 10
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# State can contain secrets (e.g. generated passwords), so refuse plain HTTP.
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# --- GitHub Actions OIDC ------------------------------------------------------------------

# Lets GitHub-issued tokens be exchanged for short-lived AWS credentials,
# so no AWS access keys are ever stored in GitHub.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

# Trust policy factory: only tokens for this repo, minted for AWS, with the given `sub`.
data "aws_iam_policy_document" "github_trust" {
  for_each = {
    plan  = "repo:${var.github_repo}:pull_request"
    apply = "repo:${var.github_repo}:${var.apply_subject}"
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [each.value]
    }
  }
}

# --- CI roles -----------------------------------------------------------------------------

resource "aws_iam_role" "ci" {
  for_each = data.aws_iam_policy_document.github_trust

  name                 = "afs-${var.env}-github-${each.key}"
  assume_role_policy   = each.value.json
  max_session_duration = 3600
}

# plan: read everything, write nothing (except the state lock file below).
resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.ci["plan"].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# `use_lockfile` takes a lock by writing <key>.tflock next to the state, so even
# a read-only plan must be able to create and delete that one object.
resource "aws_iam_role_policy" "plan_state_lock" {
  name = "state-lock"
  role = aws_iam_role.ci["plan"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:DeleteObject"]
      Resource = "${aws_s3_bucket.state.arn}/*.tflock"
    }]
  })
}

# apply: full control of this single-purpose account. The trust policy (main
# branch or protected environment only) is what limits who can use it.
resource "aws_iam_role_policy_attachment" "apply_admin" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# --- Budget ---------------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = var.budget_name
  budget_type  = "COST"
  limit_amount = var.budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [80, 100]
    content {
      notification_type          = "ACTUAL"
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.budget_email]
    }
  }
}
