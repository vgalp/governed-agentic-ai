# Kafka: Azure Event Hubs Premium through its Kafka endpoint (port 9093), private endpoint only.
# No connection strings or keys exist (local authentication off): every service signs in with its own
# managed identity and may send to or receive from exactly the event hubs that kafka_access gives it.
# The audit topics are also archived (Event Hubs Capture) into a write-once blob container.

locals {
  grants = merge([
    for s in var.services : merge(
      { for t in var.kafka_access.services[s].write : "${s}|send|${t}" => { service = s, topic = t, role = "Azure Event Hubs Data Sender" } },
      { for t in var.kafka_access.services[s].read : "${s}|receive|${t}" => { service = s, topic = t, role = "Azure Event Hubs Data Receiver" } },
    )
  ]...)
}

resource "azurerm_eventhub_namespace" "kafka" {
  name                          = "${var.name}-kafka-${var.unique}"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  sku                           = "Premium"
  capacity                      = var.capacity
  public_network_access_enabled = false
  local_authentication_enabled  = false # Entra ID only: no shared access keys to leak
  minimum_tls_version           = "1.2"
  tags                          = var.tags

  identity {
    type = "SystemAssigned" # writes the audit archive, with a role on that container only
  }

  network_rulesets {
    default_action                 = "Deny"
    public_network_access_enabled  = false
    trusted_service_access_enabled = true
  }
}

resource "azurerm_eventhub" "topic" {
  for_each     = var.kafka_access.topics
  name         = each.key
  namespace_id = azurerm_eventhub_namespace.kafka.id
  # Message text (chat, trace) is kept 7 days, never longer; the audit and approval history 90 days
  # (Premium's longest), and the audit chain is archived for good below.
  partition_count   = var.partitions
  message_retention = each.value.keep == "forever" ? 90 : 7

  dynamic "capture_description" {
    for_each = startswith(each.key, "audit.") ? [1] : []
    content {
      enabled             = true
      encoding            = "Avro"
      interval_in_seconds = 300
      size_limit_in_bytes = 314572800
      skip_empty_archives = true
      destination {
        name                = "EventHubArchive.AzureBlockBlob"
        archive_name_format = "{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}"
        blob_container_name = azurerm_storage_container.audit.name
        storage_account_id  = azurerm_storage_account.audit.id
      }
    }
  }
  depends_on = [azurerm_role_assignment.capture]
}

resource "azurerm_role_assignment" "kafka" {
  for_each             = local.grants
  scope                = azurerm_eventhub.topic[each.value.topic].id
  role_definition_name = each.value.role
  principal_id         = var.principal_ids[each.value.service]
}

module "kafka_endpoint" {
  source              = "../private-endpoint"
  name                = "${var.name}-kafka"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.endpoint_subnet_id
  resource_id         = azurerm_eventhub_namespace.kafka.id
  subresource         = "namespace"
  dns_zone_id         = var.dns_zone_ids["eventhubs"]
  tags                = var.tags
}

# --- the audit archive ----------------------------------------------------------------------------

resource "azurerm_storage_account" "audit" {
  name                            = "${replace(var.name, "-", "")}audit${var.unique}"
  location                        = var.location
  resource_group_name             = var.resource_group_name
  account_tier                    = "Standard"
  account_replication_type        = var.archive_replication
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

# Unlocked at first, so a mistake can still be corrected; lock it once capture works (deploy/README.md).
# A locked policy can be extended, never shortened or removed.
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

module "archive_endpoint" {
  source              = "../private-endpoint"
  name                = "${var.name}-audit-archive"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.endpoint_subnet_id
  resource_id         = azurerm_storage_account.audit.id
  subresource         = "blob"
  dns_zone_id         = var.dns_zone_ids["blob"]
  tags                = var.tags
}
