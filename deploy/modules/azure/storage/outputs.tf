output "account_name" {
  value = azurerm_storage_account.state.name
}

output "share_name" {
  value = azurerm_storage_share.state.name
}

output "access_key" {
  value     = azurerm_storage_account.state.primary_access_key
  sensitive = true
}
