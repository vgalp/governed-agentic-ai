output "bootstrap" {
  value = "${azurerm_eventhub_namespace.kafka.name}.servicebus.windows.net:9093"
}

output "namespace_id" {
  value = azurerm_eventhub_namespace.kafka.id
}

output "grants" {
  description = "Every Event Hubs role assignment, from ops/kafka_access.yaml."
  value       = sort(keys(local.grants))
}
