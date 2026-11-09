output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "location" {
  value = azurerm_resource_group.main.location
}

output "vnet_id" {
  value = azurerm_virtual_network.main.id
}

output "subnet_ids" {
  value = {
    apps      = azurerm_subnet.apps.id
    endpoints = azurerm_subnet.endpoints.id
    ollama    = azurerm_subnet.ollama.id
  }
}

output "subnet_cidrs" {
  value = local.cidr
}

output "dns_zone_ids" {
  description = "Private DNS zones by name: eventhubs, vault, registry, blob, file."
  value       = { for k, z in azurerm_private_dns_zone.zone : k => z.id }
}
