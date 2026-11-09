output "vpc_id" {
  value = aws_vpc.main.id
}

output "vpc_cidr" {
  value = aws_vpc.main.cidr_block
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "apps_security_group_id" {
  description = "The services' security group: Kafka, files and Ollama accept connections from it only."
  value       = aws_security_group.apps.id
}
