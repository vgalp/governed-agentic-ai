output "secret_arns" {
  value = { for k, s in aws_secretsmanager_secret.secret : k => s.arn }
}
