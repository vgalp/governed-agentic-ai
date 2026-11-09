# Placeholder outputs, used only by validate and by a plan before a dependency exists. They have the
# shape of real Azure IDs because the provider checks the format.
locals {
  rg       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/mock-rg"
  net      = "${local.rg}/providers/Microsoft.Network"
  ident    = "${local.rg}/providers/Microsoft.ManagedIdentity/userAssignedIdentities"
  zone     = "${local.net}/privateDnsZones"
  uuid     = "00000000-0000-0000-0000-000000000000"
  commands = ["init", "validate", "plan"]

  network = {
    resource_group_name = "mock-rg"
    location            = "eastus2"
    vnet_id             = "${local.net}/virtualNetworks/mock"
    subnet_ids = {
      apps      = "${local.net}/virtualNetworks/mock/subnets/apps"
      endpoints = "${local.net}/virtualNetworks/mock/subnets/private-endpoints"
      ollama    = "${local.net}/virtualNetworks/mock/subnets/ollama"
    }
    subnet_cidrs = { apps = "10.0.0.0/23", "private-endpoints" = "10.0.2.0/23", ollama = "10.0.4.0/23" }
    dns_zone_ids = {
      eventhubs = "${local.zone}/privatelink.servicebus.windows.net"
      vault     = "${local.zone}/privatelink.vaultcore.azure.net"
      registry  = "${local.zone}/privatelink.azurecr.io"
      blob      = "${local.zone}/privatelink.blob.core.windows.net"
      file      = "${local.zone}/privatelink.file.core.windows.net"
    }
  }
  identities = {
    ids           = { for s in ["api", "planner", "executor"] : s => "${local.ident}/mock-${s}" }
    principal_ids = { for s in ["api", "planner", "executor"] : s => local.uuid }
    client_ids    = { for s in ["api", "planner", "executor"] : s => local.uuid }
  }
  kafka    = { bootstrap = "mock.servicebus.windows.net:9093" }
  secrets  = { secret_ids = { "audit-hmac-key" = "https://mock.vault.azure.net/secrets/audit-hmac-key", "opa-token" = "https://mock.vault.azure.net/secrets/opa-token" } }
  registry = { login_server = "mock.azurecr.io" }
  storage  = { account_name = "mockstate", share_name = "state", access_key = "bW9jaw==" }
  ollama   = { host = "http://10.0.4.4:11434", private_ip = "10.0.4.4" }
}
