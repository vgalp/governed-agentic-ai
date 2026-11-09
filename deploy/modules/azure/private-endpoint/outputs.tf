output "id" {
  value = azurerm_private_endpoint.this.id
}

output "private_ip" {
  value = azurerm_private_endpoint.this.private_service_connection[0].private_ip_address
}
