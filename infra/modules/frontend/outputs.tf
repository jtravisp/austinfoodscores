output "url" {
  value = "https://${coalesce(var.domain, aws_cloudfront_distribution.this.domain_name)}"
}

output "site_bucket" {
  value = aws_s3_bucket.site.bucket
}

output "distribution_id" {
  value = aws_cloudfront_distribution.this.id
}
