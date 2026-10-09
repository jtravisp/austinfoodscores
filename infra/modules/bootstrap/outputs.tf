output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "plan_role_arn" {
  value = aws_iam_role.ci["plan"].arn
}

output "apply_role_arn" {
  value = aws_iam_role.ci["apply"].arn
}

output "socrata_token_parameter" {
  value = aws_ssm_parameter.socrata_token.name
}
