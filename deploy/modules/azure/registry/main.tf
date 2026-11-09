# The container registry (Premium: private endpoints). No admin user, no anonymous pulls. Each service
# identity may pull.

resource "azurerm_container_registry" "main" {
  name                          = "${replace(var.name, "-", "")}acr${var.unique}"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  sku                           = "Premium"
  admin_enabled                 = false
  anonymous_pull_enabled        = false
  public_network_access_enabled = length(var.operator_cidrs) > 0
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

module "endpoint" {
  source              = "../private-endpoint"
  name                = "${var.name}-registry"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.endpoint_subnet_id
  resource_id         = azurerm_container_registry.main.id
  subresource         = "registry"
  dns_zone_id         = var.dns_zone_ids["registry"]
  tags                = var.tags
}

resource "azurerm_role_assignment" "pull" {
  for_each             = var.principal_ids
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = each.value
}
