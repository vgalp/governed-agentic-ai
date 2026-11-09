# One virtual network, no public endpoints. Subnets:
#   apps               the internal Container Apps environment (delegated)
#   private-endpoints  Event Hubs, Key Vault, the registry and storage, each reachable only here
#   ollama             the GPU VM, which answers only the apps subnet
# Outbound internet only through the NAT gateway, and only when internet_egress = true; subnets
# have no default outbound access either way.

resource "azurerm_resource_group" "main" {
  name     = "${var.name}-rg"
  location = var.location
  tags     = var.tags
}

locals {
  cidr = { for i, n in ["apps", "private-endpoints", "ollama"] : n => cidrsubnet(var.address_space, 7, i) }
}

resource "azurerm_virtual_network" "main" {
  name                = "${var.name}-vnet"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = [var.address_space]
  tags                = var.tags
}

resource "azurerm_subnet" "apps" {
  name                            = "apps"
  resource_group_name             = azurerm_resource_group.main.name
  virtual_network_name            = azurerm_virtual_network.main.name
  address_prefixes                = [local.cidr["apps"]]
  default_outbound_access_enabled = false
  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "endpoints" {
  name                              = "private-endpoints"
  resource_group_name               = azurerm_resource_group.main.name
  virtual_network_name              = azurerm_virtual_network.main.name
  address_prefixes                  = [local.cidr["private-endpoints"]]
  default_outbound_access_enabled   = false
  private_endpoint_network_policies = "Enabled" # the security group below applies to the endpoints
}

resource "azurerm_subnet" "ollama" {
  name                            = "ollama"
  resource_group_name             = azurerm_resource_group.main.name
  virtual_network_name            = azurerm_virtual_network.main.name
  address_prefixes                = [local.cidr["ollama"]]
  default_outbound_access_enabled = false
}

# --- who may talk to whom -------------------------------------------------------------------------

resource "azurerm_network_security_group" "endpoints" {
  name                = "${var.name}-endpoints-nsg"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags

  security_rule {
    name                       = "apps-to-services"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = local.cidr["apps"]
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_ranges    = ["443", "445", "9093"] # HTTPS, Azure Files, Kafka
  }
  security_rule {
    name                       = "operators-to-registry-and-vault"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = "VirtualNetwork" # a peered network, VPN or ExpressRoute
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "443"
  }
  security_rule {
    name                       = "nothing-else"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "endpoints" {
  subnet_id                 = azurerm_subnet.endpoints.id
  network_security_group_id = azurerm_network_security_group.endpoints.id
}

resource "azurerm_network_security_group" "ollama" {
  name                = "${var.name}-ollama-nsg"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags

  security_rule {
    name                       = "apps-to-ollama"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = local.cidr["apps"]
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "11434" # Ollama has no sign-in of its own: only the apps may reach it
  }
  security_rule {
    name                       = "admin-ssh-from-the-network"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = "VirtualNetwork"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "22"
  }
  security_rule {
    name                       = "nothing-else"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "ollama" {
  subnet_id                 = azurerm_subnet.ollama.id
  network_security_group_id = azurerm_network_security_group.ollama.id
}

# --- outbound only, when allowed ------------------------------------------------------------------

resource "azurerm_public_ip" "nat" {
  count               = var.internet_egress ? 1 : 0
  name                = "${var.name}-nat-ip"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway" "main" {
  count               = var.internet_egress ? 1 : 0
  name                = "${var.name}-nat"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  sku_name            = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway_public_ip_association" "main" {
  count                = var.internet_egress ? 1 : 0
  nat_gateway_id       = azurerm_nat_gateway.main[0].id
  public_ip_address_id = azurerm_public_ip.nat[0].id
}

resource "azurerm_subnet_nat_gateway_association" "egress" {
  for_each       = var.internet_egress ? { apps = azurerm_subnet.apps.id, ollama = azurerm_subnet.ollama.id } : {}
  subnet_id      = each.value
  nat_gateway_id = azurerm_nat_gateway.main[0].id
}

# --- private DNS: the services' usual names resolve to their private endpoints --------------------

locals {
  dns_zones = {
    eventhubs = "privatelink.servicebus.windows.net"
    vault     = "privatelink.vaultcore.azure.net"
    registry  = "privatelink.azurecr.io"
    blob      = "privatelink.blob.core.windows.net"
    file      = "privatelink.file.core.windows.net"
  }
}

resource "azurerm_private_dns_zone" "zone" {
  for_each            = local.dns_zones
  name                = each.value
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "zone" {
  for_each              = local.dns_zones
  name                  = "${var.name}-${each.key}"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.zone[each.key].name
  virtual_network_id    = azurerm_virtual_network.main.id
}
