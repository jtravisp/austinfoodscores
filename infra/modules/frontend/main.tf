# The public site: one CloudFront distribution, two origins.
#   /*      -> private S3 bucket (static map), read via Origin Access Control
#   /api/*  -> API Gateway (read API), cached at the edge per query string
# Page and API share one origin, so the browser never makes a CORS request.
# Modeled on ../armybandncoer/infra/prod/site.tf.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  custom_domain = var.domain != null
  api_host      = trimprefix(var.api_endpoint, "https://")
}

# --- site bucket ---------------------------------------------------------------------

resource "aws_s3_bucket" "site" {
  bucket        = "${var.name}-site-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.force_destroy
}

# Deliberately NOT aws_s3_bucket_website_configuration: the website endpoint is
# a separate public HTTP endpoint that can't use OAC. The REST endpoint + OAC is
# what makes "only this distribution can read the bucket" true.
resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A bad deploy is one sync away; versioning makes it recoverable.
resource "aws_s3_bucket_versioning" "site" {
  bucket = aws_s3_bucket.site.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    id     = "expire-old-site-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Read access for exactly one distribution. The CloudFront service principal is
# shared by every AWS customer; the SourceArn condition is what makes it ours.
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowThisDistributionRead"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.site.arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.this.arn } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.site.arn, "${aws_s3_bucket.site.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.site]
}

# --- certificate + DNS (prod only: when var.domain is set) -------------------------------

# Found by name, not remote state: infra/dns created it, and the name never changes.
data "aws_route53_zone" "this" {
  count        = local.custom_domain ? 1 : 0
  name         = "${var.domain}."
  private_zone = false
}

# CloudFront only accepts certificates from us-east-1, which is our region anyway.
resource "aws_acm_certificate" "this" {
  count             = local.custom_domain ? 1 : 0
  domain_name       = var.domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true # a replacement must validate before the old one detaches
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.custom_domain ? {
    for dvo in aws_acm_certificate.this[0].domain_validation_options : dvo.domain_name => dvo
  } : {}

  zone_id         = data.aws_route53_zone.this[0].zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  ttl             = 60
  records         = [each.value.resource_record_value]
  allow_overwrite = true
}

# Waits until ACM reports the certificate issued, so the distribution never
# references a pending certificate.
resource "aws_acm_certificate_validation" "this" {
  count                   = local.custom_domain ? 1 : 0
  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# Alias records (not CNAMEs): work at a zone apex, free to query, one lookup.
resource "aws_route53_record" "alias" {
  for_each = local.custom_domain ? toset(["A", "AAAA"]) : toset([])

  zone_id = data.aws_route53_zone.this[0].zone_id
  name    = var.domain
  type    = each.value
  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}

# --- CloudFront --------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.name}-site"
  description                       = "Signs CloudFront requests to the site bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# AWS-managed policies, looked up by name (their IDs mean nothing to a reader).
data "aws_cloudfront_cache_policy" "static" {
  name = "Managed-CachingOptimized"
}

# Our own API cache policy. The managed "UseOriginCacheControlHeaders-QueryStrings"
# looks right by name but keys on (and so forwards) the Host header, which API
# Gateway rejects, and on all cookies, which fragments the cache. This one keys on
# the query string only; TTL follows the API's Cache-Control (max-age=300).
resource "aws_cloudfront_cache_policy" "api" {
  name        = "${var.name}-api"
  comment     = "Read API: cache per query string, honor origin Cache-Control"
  min_ttl     = 0
  default_ttl = 0    # no Cache-Control from the origin (errors) -> don't cache
  max_ttl     = 3600 # cap, whatever the origin says

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    headers_config {
      header_behavior = "none"
    }
    cookies_config {
      cookie_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "all"
    }
  }
}

# Forward everything except Host: API Gateway routes on its own hostname and
# rejects requests carrying ours.
data "aws_cloudfront_origin_request_policy" "api" {
  name = "Managed-AllViewerExceptHostHeader"
}

# Security headers + our CSP. The CSP text is shared with `afs serve`, so a
# violation shows up locally before it ships.
resource "aws_cloudfront_response_headers_policy" "this" {
  name    = "${var.name}-headers"
  comment = "CSP and security headers for the Austin Food Scores site"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = false
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      # OSM's tile policy expects a Referer; this sends the origin only.
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    content_security_policy {
      content_security_policy = trimspace(file("${path.module}/csp.txt"))
      override                = true
    }
  }
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  comment             = "${var.name} site"
  default_root_object = "index.html"
  aliases             = local.custom_domain ? [var.domain] : []
  price_class         = "PriceClass_100" # North America + Europe edges; our visitors are in Austin

  origin {
    origin_id                = "site"
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  origin {
    origin_id   = "api"
    domain_name = local.api_host
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id           = "site"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.static.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.this.id
  }

  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "api"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.api.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.api.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.this.id
  }

  # No custom_error_response: those apply to every origin and would turn the
  # API's JSON 404s into HTML pages. The app routes with #hashes, so S3 only
  # ever needs to serve real files.

  viewer_certificate {
    cloudfront_default_certificate = !local.custom_domain
    acm_certificate_arn            = local.custom_domain ? aws_acm_certificate_validation.this[0].certificate_arn : null
    ssl_support_method             = local.custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.custom_domain ? "TLSv1.2_2021" : "TLSv1"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
}
