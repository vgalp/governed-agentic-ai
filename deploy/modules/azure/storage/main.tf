# Shared files for the services: audit checkpoints and worker health. Container Apps mounts Azure
# Files with the account key, so this account holds nothing else and is reachable only through its
# private endpoint.

resource "azurerm_storage_account" "state" {
  name                            = "${replace(var.name, "-", "")}state${var.unique}"
  location                        = var.location
  resource_group_name             = var.resource_group_name
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
  quota              = var.quota_gb
}

module "endpoint" {
  source              = "../private-endpoint"
  name                = "${var.name}-state"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.endpoint_subnet_id
  resource_id         = azurerm_storage_account.state.id
  subresource         = "file"
  dns_zone_id         = var.dns_zone_ids["file"]
  tags                = var.tags
}
