# Alarms -> SNS -> email. Used by prod.
#
# Each alarm watches a symptom someone should act on: a Lambda erroring, the
# weekly schedule failing to invoke fetch, the public API returning 5xx, or
# the database running hot or out of disk. treat_missing_data = notBreaching:
# no traffic is not a problem.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

# Email subscriptions stay "PendingConfirmation" until the link in AWS's
# confirmation email is clicked; until then nothing is delivered.
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  actions = [aws_sns_topic.alerts.arn]
}

# Any error from fetch, load, or query. Load also errors on purpose when more
# than 1% of rows are rejected (an upstream format change).
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = var.function_names

  alarm_name          = "${var.name}-${each.key}-errors"
  alarm_description   = "Lambda ${each.value} reported errors. Check /aws/lambda/${each.value}."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# The scheduler tried to invoke fetch and the invocation failed (e.g. a broken
# role), which never shows up as a Lambda error because the Lambda never ran.
resource "aws_cloudwatch_metric_alarm" "schedule_failures" {
  alarm_name          = "${var.name}-schedule-target-errors"
  alarm_description   = "EventBridge Scheduler could not invoke the weekly fetch."
  namespace           = "AWS/Scheduler"
  metric_name         = "TargetErrorCount"
  dimensions          = { ScheduleGroup = "default" }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.name}-api-5xx"
  alarm_description   = "The read API returned 5 or more server errors in 5 minutes."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = var.api_id, Stage = "$default" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name}-rds-cpu"
  alarm_description   = "RDS CPU above 80% for 15 minutes. t4g instances also burn CPU credits."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${var.name}-rds-free-storage"
  alarm_description   = "Less than 2 GB of RDS storage left."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 2 * 1024 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # no storage metric at all means the instance is gone
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

variable "name" {
  type = string
}

variable "alarm_email" {
  type = string
}

variable "function_names" {
  description = "Short label -> Lambda function name, e.g. { fetch = \"afs-prod-fetch\" }."
  type        = map(string)
}

variable "api_id" {
  type = string
}

variable "db_identifier" {
  type = string
}

output "topic_arn" {
  value = aws_sns_topic.alerts.arn
}
