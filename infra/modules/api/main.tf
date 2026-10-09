# Public read API: API Gateway HTTP API -> query Lambda (in VPC, afs_reader) -> RDS.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

module "query" {
  source = "../lambda_function"

  name        = "${var.name}-query"
  handler     = "afs.handlers.query.handler"
  zip_path    = var.lambda_zip
  timeout     = 10
  memory_size = 512 # Lambda CPU scales with memory; serializing + gzipping ~2 MB of GeoJSON benefits

  environment = merge(var.connection_env, { DB_USER = "afs_reader" })

  vpc = {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.lambda_security_group_id]
  }

  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "${var.dbuser_arn_prefix}/afs_reader"
    }]
  })
}

# HTTP API (v2): cheaper and simpler than REST APIs.
#
# No CORS configuration: the browser reaches this API through the site's own
# CloudFront distribution under /api/*, so page and API share one origin and
# the browser never makes a cross-origin request. Routes therefore carry the
# /api prefix themselves; CloudFront forwards the path unchanged.
resource "aws_apigatewayv2_api" "this" {
  name          = var.name
  protocol_type = "HTTP"
}

# AWS_PROXY: the whole request goes to Lambda as an event and its return value
# becomes the response. Payload format 2.0 is the leaner HTTP API event shape.
resource "aws_apigatewayv2_integration" "query" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.query.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "this" {
  for_each = toset([
    "GET /api/establishments",
    "GET /api/establishments/{facility_id}",
    "GET /api/stats/decliners",
  ])

  api_id    = aws_apigatewayv2_api.this.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.query.id}"
}

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${var.name}"
  retention_in_days = 14
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default" # serves at the root URL, no /stage prefix
  auto_deploy = true

  # A public endpoint: cap request rate so a crawler or loop can't run up
  # Lambda costs or exhaust the t4g.micro's connections.
  default_route_settings {
    throttling_rate_limit  = var.throttle_rate
    throttling_burst_limit = var.throttle_burst
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      latencyMs      = "$context.responseLatency"
      integrationErr = "$context.integrationErrorMessage"
      responseBytes  = "$context.responseLength"
    })
  }
}

# Resource-based policy: let this API (any stage, any route) invoke the function.
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.query.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
