output "vault_id" {
  value = azurerm_key_vault.main.id
}

output "secret_ids" {
  description = "Versionless secret IDs by name, for Container Apps secret references."
  value       = { for k, s in azurerm_key_vault_secret.secret : k => s.versionless_id }
}
