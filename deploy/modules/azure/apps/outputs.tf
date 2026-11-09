output "api_url" {
  description = "The web app, reachable only from inside the network (or a network connected to it)."
  value       = "https://${azurerm_container_app.svc["api"].ingress[0].fqdn}"
}

output "environment_id" {
  value = azurerm_container_app_environment.main.id
}
