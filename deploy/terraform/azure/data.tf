# Secrets, the image registry and storage. All with public network access off (the registry and the
# vault open only to operator_cidrs while you deploy, if you set any).

data "azurerm_client_config" "current" {}

locals {
  operator_access = length(var.operator_cidrs) > 0
}

# --- Key Vault: the audit key and OPA's token ------------------------------------------------------

resource "azurerm_key_vault" "main" {
  name                          = "${var.name}-kv-${local.suffix}"
  location                      = var.location
  resource_group_name           = azurerm_resource_group.main.name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  enable_rbac_authorization     = true
  purge_protection_enabled      = true # a deleted audit key would make the whole audit chain unverifiable
  soft_delete_retention_days    = 90
  public_network_access_enabled = local.operator_access
  tags                          = var.tags

  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
    ip_rules       = var.operator_cidrs
  }
}

resource "azurerm_role_assignment" "deployer_secrets" {
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
  depends_on   = [azurerm_role_assignment.deployer_secrets, azurerm_private_endpoint.pe]
}

resource "azurerm_role_assignment" "svc_secrets" {
  for_each             = azurerm_user_assigned_identity.svc
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value.principal_id
}

# --- Container registry ---------------------------------------------------------------------------

resource "azurerm_container_registry" "main" {
  name                          = "${var.name}acr${local.suffix}"
  location                      = var.location
  resource_group_name           = azurerm_resource_group.main.name
  sku                           = "Premium" # private endpoints need Premium
  admin_enabled                 = false
  anonymous_pull_enabled        = false
  public_network_access_enabled = local.operator_access
  network_rule_bypass_option    = "None"
  tags                          = var.tags

  network_rule_set {
    default_action = "Deny"
    ip_rule = [for cidr in var.operator_cidrs : {
      action   = "Allow"
      ip_range = cidr
    }]
  }
}

resource "azurerm_role_assignment" "svc_pull" {
  for_each             = azurerm_user_assigned_identity.svc
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = each.value.principal_id
}

# --- The audit archive: Event Hubs capture into a write-once container -----------------------------

resource "azurerm_storage_account" "audit" {
  name                            = "${var.name}audit${local.suffix}"
  location                        = var.location
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  public_network_access_enabled   = false
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  tags                            = var.tags

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
  }

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"] # Event Hubs capture is a trusted service
  }
}

resource "azurerm_storage_container" "audit" {
  name                  = "audit-archive"
  storage_account_id    = azurerm_storage_account.audit.id
  container_access_type = "private"
}

# Unlocked at first, so a mistake can still be corrected; lock it once capture works (README). A
# locked policy can be extended, never shortened or removed.
resource "azurerm_storage_container_immutability_policy" "audit" {
  storage_container_resource_manager_id = azurerm_storage_container.audit.id
  immutability_period_in_days           = var.audit_immutability_days
  protected_append_writes_enabled       = true
}

resource "azurerm_role_assignment" "capture" {
  scope                = azurerm_storage_container.audit.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_eventhub_namespace.kafka.identity[0].principal_id
}

# --- Shared files: audit checkpoints and worker health ---------------------------------------------
# Container Apps mounts Azure Files with the account key, so this account is separate from the audit
# archive, holds nothing else, and is reachable only through its private endpoint.

resource "azurerm_storage_account" "state" {
  name                            = "${var.name}state${local.suffix}"
  location                        = var.location
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  public_network_access_enabled   = false
  allow_nested_items_to_be_public = false
  tags                            = var.tags

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }
}

resource "azurerm_storage_share" "state" {
  name               = "state"
  storage_account_id = azurerm_storage_account.state.id
  quota              = 10
}
