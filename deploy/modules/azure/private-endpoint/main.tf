# One private endpoint with its DNS record: the service's usual name resolves to a private address
# in the network, and the service itself has public access turned off.

resource "azurerm_private_endpoint" "this" {
  name                = "${var.name}-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = var.name
    private_connection_resource_id = var.resource_id
    subresource_names              = [var.subresource]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.dns_zone_id]
  }
}
