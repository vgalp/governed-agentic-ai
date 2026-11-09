# One managed identity per service. Kafka, the registry and the vault each give these identities only
# what that service needs (kafka, registry and secrets modules).

resource "azurerm_user_assigned_identity" "svc" {
  for_each            = toset(var.services)
  name                = "${var.name}-${each.key}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}
