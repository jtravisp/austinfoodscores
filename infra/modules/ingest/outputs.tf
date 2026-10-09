output "raw_bucket" {
  value = aws_s3_bucket.raw.bucket
}

output "fetch_function_name" {
  value = module.fetch.function_name
}

output "load_function_name" {
  value = module.load.function_name
}
