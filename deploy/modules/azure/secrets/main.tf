# Key Vault with the audit key and OPA's token, generated here. Private endpoint only (operator_cidrs
# while deploying, if set). Each service's identity may read the secrets, nothing more.

data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "main" {
  name                          = "${var.name}-kv-${var.unique}"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true # a deleted audit key would make the whole audit chain unverifiable
  soft_delete_retention_days    = 90
  public_network_access_enabled = length(var.operator_cidrs) > 0
  tags                          = var.tags

  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
    ip_rules       = var.operator_cidrs
  }
}

module "endpoint" {
  source              = "../private-endpoint"
  name                = "${var.name}-vault"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.endpoint_subnet_id
  resource_id         = azurerm_key_vault.main.id
  subresource         = "vault"
  dns_zone_id         = var.dns_zone_ids["vault"]
  tags                = var.tags
}

resource "azurerm_role_assignment" "deployer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "random_id" "audit_key" {
  byte_length = 32
}

resource "random_id" "opa_token" {
  byte_length = 24
}

resource "azurerm_key_vault_secret" "secret" {
  for_each = {
    audit-hmac-key = random_id.audit_key.hex # signs every audit event and checkpoint (ADR 002, 030)
    opa-token      = random_id.opa_token.hex # the only caller OPA answers (ADR 030)
  }
  name         = each.key
  value        = each.value
  key_vault_id = azurerm_key_vault.main.id
  content_type = "text/plain"
  depends_on   = [azurerm_role_assignment.deployer, module.endpoint]
}

resource "azurerm_role_assignment" "svc" {
  for_each             = var.principal_ids
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value
}
