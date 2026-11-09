output "api_url" {
  description = "The web app, on the internal load balancer: reachable only from client_cidrs."
  value       = "${var.certificate_arn == "" ? "http" : "https"}://${aws_lb.api.dns_name}"
}

output "setup_command" {
  description = "Creates the Kafka topics once, from inside the VPC."
  value = join(" ", [
    "aws ecs run-task --region ${local.region} --cluster ${aws_ecs_cluster.main.name}",
    "--task-definition ${aws_ecs_task_definition.setup.family} --launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", var.subnet_ids)}],securityGroups=[${var.apps_security_group_id}],assignPublicIp=DISABLED}'",
  ])
}
