output "api_url" {
  description = "The web app, reachable only from inside the network (or a network peered with it)."
  value       = var.deploy_apps ? "https://${azurerm_container_app.svc["api"].ingress[0].fqdn}" : "(deploy_apps = false)"
}

output "registry" {
  description = "Push the image here: docker push <registry>/governed-agentic-ai:<image_tag>"
  value       = azurerm_container_registry.main.login_server
}

output "kafka_bootstrap" {
  value = local.kafka_bootstrap
}

output "ollama_private_ip" {
  value = azurerm_network_interface.ollama.private_ip_address
}

output "service_identities" {
  description = "Client IDs of the services' managed identities (Kafka, registry and vault access)."
  value       = { for k, v in azurerm_user_assigned_identity.svc : k => v.client_id }
}

output "kafka_grants" {
  description = "Every Event Hubs role assignment, from ops/kafka_access.yaml."
  value       = sort(keys(local.grants))
}
