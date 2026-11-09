# Kafka: Azure Event Hubs Premium through its Kafka endpoint (port 9093), private endpoint only.
# No connection strings or keys exist (local authentication off): every service signs in with its own
# managed identity, and may send to or receive from exactly the event hubs that ops/kafka_access.yaml
# gives it (Azure Event Hubs Data Sender / Data Receiver on each hub).

locals {
  access = yamldecode(file("${path.module}/../../../ops/kafka_access.yaml"))
  # The services this deployment runs. audit.verify is run by an operator with their own sign-in.
  deployed = ["api", "planner", "executor"]
  grants = merge([
    for s in local.deployed : merge(
      { for t in flatten(try(local.access.services[s].write, [])) : "${s}|send|${t}" => { service = s, topic = t, role = "Azure Event Hubs Data Sender" } },
      { for t in flatten(try(local.access.services[s].read, [])) : "${s}|receive|${t}" => { service = s, topic = t, role = "Azure Event Hubs Data Receiver" } },
    )
  ]...)
  kafka_bootstrap = "${azurerm_eventhub_namespace.kafka.name}.servicebus.windows.net:9093"
}

resource "azurerm_eventhub_namespace" "kafka" {
  name                          = "${var.name}-kafka-${local.suffix}"
  location                      = var.location
  resource_group_name           = azurerm_resource_group.main.name
  sku                           = "Premium"
  capacity                      = var.eventhubs_capacity
  public_network_access_enabled = false
  local_authentication_enabled  = false # Entra ID only: no shared access keys to leak
  minimum_tls_version           = "1.2"
  tags                          = var.tags

  identity {
    type = "SystemAssigned" # writes the audit archive (capture), with a role on that container only
  }

  network_rulesets {
    default_action                 = "Deny"
    public_network_access_enabled  = false
    trusted_service_access_enabled = true
  }
}

resource "azurerm_eventhub" "topic" {
  for_each     = local.access.topics
  name         = each.key
  namespace_id = azurerm_eventhub_namespace.kafka.id
  # Message text (chat, trace) is kept 7 days, never longer. The audit and approval history is kept
  # 90 days in Event Hubs (Premium's longest) and, for the audit chain, archived for good below.
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
}

resource "azurerm_user_assigned_identity" "svc" {
  for_each            = toset(local.deployed)
  name                = "${var.name}-${each.key}"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_role_assignment" "kafka" {
  for_each             = local.grants
  scope                = azurerm_eventhub.topic[each.value.topic].id
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.svc[each.value.service].principal_id
}
