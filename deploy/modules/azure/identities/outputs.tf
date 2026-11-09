output "ids" {
  value = { for k, v in azurerm_user_assigned_identity.svc : k => v.id }
}

output "principal_ids" {
  value = { for k, v in azurerm_user_assigned_identity.svc : k => v.principal_id }
}

output "client_ids" {
  value = { for k, v in azurerm_user_assigned_identity.svc : k => v.client_id }
}
