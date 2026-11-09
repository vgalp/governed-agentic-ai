output "api_url" {
  description = "The web app, on the internal load balancer: reachable only from client_cidrs inside or connected to the VPC."
  value       = "${var.certificate_arn == "" ? "http" : "https"}://${aws_lb.api.dns_name}"
}

output "registry" {
  description = "Push the image here: docker push <registry>:<image_tag>"
  value       = aws_ecr_repository.app.repository_url
}

output "kafka_bootstrap" {
  value = local.kafka_bootstrap
}

output "ollama_private_ip" {
  value = aws_instance.ollama.private_ip
}

output "setup_command" {
  description = "Creates the Kafka topics once, from inside the VPC (run after the first apply with deploy_apps = true)."
  value = var.deploy_apps ? join(" ", [
    "aws ecs run-task --region ${var.region} --cluster ${aws_ecs_cluster.main.name}",
    "--task-definition ${aws_ecs_task_definition.setup[0].family} --launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.private[*].id)}],securityGroups=[${aws_security_group.apps.id}],assignPublicIp=DISABLED}'",
  ]) : "(deploy_apps = false)"
}

output "kafka_policies" {
  description = "Each service's Kafka permissions, from ops/kafka_access.yaml."
  value       = { for k, v in local.kafka_policy : k => [for s in v.Statement : s.Sid] }
}
