output "private_ip" {
  value = azurerm_network_interface.ollama.private_ip_address
}

output "host" {
  description = "OLLAMA_HOST for the services."
  value       = "http://${azurerm_network_interface.ollama.private_ip_address}:11434"
}
