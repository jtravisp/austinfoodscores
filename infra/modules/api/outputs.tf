output "endpoint" {
  description = "Base URL, e.g. https://abc123.execute-api.us-east-1.amazonaws.com"
  value       = aws_apigatewayv2_api.this.api_endpoint
}

output "query_function_name" {
  value = module.query.function_name
}

output "api_id" {
  value = aws_apigatewayv2_api.this.id
}
