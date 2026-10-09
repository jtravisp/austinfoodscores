# Private-only VPC. No internet gateway, no NAT: nothing here is reachable from
# the internet, and nothing here can reach the internet. In-VPC Lambdas reach
# S3 through a gateway endpoint (free) and RDS through security groups.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_region" "current" {}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}

resource "aws_vpc" "this" {
  cidr_block = var.cidr

  # Needed for RDS endpoint hostnames and VPC endpoints to resolve inside the VPC.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

# --- Subnets and routing --------------------------------------------------------------

# RDS requires a subnet group spanning at least two AZs, even for a single-AZ
# instance, and Lambda spreads ENIs across the subnets it's given.
resource "aws_subnet" "private" {
  for_each = { for i, az in local.azs : az => i }

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.cidr, 8, each.value) # /16 -> /24: 10.x.0.0/24, 10.x.1.0/24

  tags = { Name = "${var.name}-private-${each.key}" }
}

# One route table for all private subnets. It has only the implicit "local"
# route (VPC-internal) plus the S3 endpoint route added below: no 0.0.0.0/0.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# A gateway endpoint adds a route to S3's public IP ranges (an AWS-managed
# prefix list) that stays on the AWS network. Gateway endpoints are free;
# interface endpoints (needed for most other services) cost ~$7/month per AZ.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${var.name}-s3" }
}

# --- Security groups --------------------------------------------------------------------

# The default SG allows all traffic between its members. Managing it with no
# rules strips that, so anything accidentally launched with it is isolated.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-default-locked" }
}

resource "aws_security_group" "lambda" {
  name        = "${var.name}-lambda"
  description = "In-VPC Lambdas (load, query)"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-lambda" }
}

resource "aws_security_group" "rds" {
  name        = "${var.name}-rds"
  description = "Postgres; reachable only from the Lambda SG"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-rds" }
}

# Security groups are stateful: reply traffic is allowed automatically, so the
# RDS SG needs no egress rules and the Lambda SG needs no ingress rules.
resource "aws_vpc_security_group_ingress_rule" "rds_from_lambda" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Postgres from Lambdas"
}

# Lambda egress is an allow-list instead of the usual "everything":
# Postgres to the RDS SG, and HTTPS to S3 via the endpoint's prefix list.
resource "aws_vpc_security_group_egress_rule" "lambda_to_rds" {
  security_group_id            = aws_security_group.lambda.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Postgres"
}

resource "aws_vpc_security_group_egress_rule" "lambda_to_s3" {
  security_group_id = aws_security_group.lambda.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "S3 via gateway endpoint"
}

# --- Flow logs (optional; on in prod) ---------------------------------------------------------

resource "aws_cloudwatch_log_group" "flow" {
  count = var.flow_logs_enabled ? 1 : 0

  name              = "/vpc/${var.name}/flow-logs"
  retention_in_days = var.flow_log_retention_days
}

data "aws_iam_policy_document" "flow_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow" {
  count = var.flow_logs_enabled ? 1 : 0

  name               = "${var.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
}

resource "aws_iam_role_policy" "flow" {
  count = var.flow_logs_enabled ? 1 : 0

  name = "write-flow-logs"
  role = aws_iam_role.flow[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow[0].arn}:*"
    }]
  })
}

resource "aws_flow_log" "this" {
  count = var.flow_logs_enabled ? 1 : 0

  vpc_id          = aws_vpc.this.id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.flow[0].arn
  iam_role_arn    = aws_iam_role.flow[0].arn
}
