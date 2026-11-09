output "bootstrap" {
  value = aws_msk_cluster.main.bootstrap_brokers_sasl_iam
}

output "cluster_arn" {
  value = aws_msk_cluster.main.arn
}

output "replication" {
  value = min(3, local.az_count)
}

output "policies" {
  description = "Each service's Kafka policy (JSON), from ops/kafka_access.yaml."
  value       = { for k, v in local.policies : k => jsonencode(v) }
}

output "setup_policy" {
  value = jsonencode(local.setup_policy)
}
