output "address" {
  value = aws_db_instance.this.address
}

output "connection_env" {
  description = "DB_HOST/DB_PORT/DB_NAME for Lambda environments."
  value       = local.connection_env
}

output "dbuser_arn_prefix" {
  description = "Append /<db user> to get the rds-db:connect resource ARN for that user."
  value       = local.dbuser_arn_prefix
}

output "master_secret_arn" {
  value = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "migrate_function_name" {
  value = module.migrate.function_name
}
