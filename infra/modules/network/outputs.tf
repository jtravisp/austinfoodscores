output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  value = [for s in aws_subnet.private : s.id]
}

output "lambda_security_group_id" {
  value = aws_security_group.lambda.id
}

output "rds_security_group_id" {
  value = aws_security_group.rds.id
}
