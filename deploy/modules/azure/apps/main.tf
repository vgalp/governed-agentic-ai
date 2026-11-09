# The services, in an internal Container Apps environment: its only address is a private IP in the
# apps subnet, and its names resolve only inside the network.
#
#   api       the web app, dashboard and /health (port 8000, reachable from the network)
#   planner   the planner, the tool servers and OPA, sharing 127.0.0.1 as on one machine
#   executor  the executor, the tool servers and OPA
#
# Each app runs as its own managed identity (identities module): Kafka, registry and vault access
# were granted to that identity by the other modules. One replica each: the executor rebuilds its
# state from Kafka and must not run twice.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.name}-logs"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_container_app_environment" "main" {
  name                               = "${var.name}-apps"
  location                           = var.location
  resource_group_name                = var.resource_group_name
  log_analytics_workspace_id         = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id           = var.apps_subnet_id
  internal_load_balancer_enabled     = true
  infrastructure_resource_group_name = "${var.name}-apps-managed-rg"
  tags                               = var.tags

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

resource "azurerm_private_dns_zone" "apps" {
  name                = azurerm_container_app_environment.main.default_domain
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "apps" {
  name                  = "${var.name}-apps"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.apps.name
  virtual_network_id    = var.vnet_id
}

resource "azurerm_private_dns_a_record" "apps" {
  name                = "*"
  zone_name           = azurerm_private_dns_zone.apps.name
  resource_group_name = var.resource_group_name
  ttl                 = 300
  records             = [azurerm_container_app_environment.main.static_ip_address]
}

resource "azurerm_container_app_environment_storage" "state" {
  name                         = "state"
  container_app_environment_id = azurerm_container_app_environment.main.id
  account_name                 = var.state_account_name
  share_name                   = var.state_share_name
  access_key                   = var.state_access_key
  access_mode                  = "ReadWrite"
}

locals {
  opa = {
    cmd    = ["opa", "run", "--server", "--addr", "127.0.0.1:8181", "--authentication=token", "--authorization=basic", "/app/policies"]
    cpu    = 0.25
    memory = "0.5Gi"
  }
  tools = { cmd = ["python", "-m", "ops.run_all"], cpu = 0.5, memory = "1Gi" }
  apps = {
    api      = { api = { cmd = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"], cpu = 1.0, memory = "2Gi" } }
    planner  = { planner = { cmd = ["python", "-m", "agents.planner"], cpu = 1.25, memory = "2.5Gi" }, tools = local.tools, opa = local.opa }
    executor = { executor = { cmd = ["python", "-m", "agents.executor"], cpu = 0.5, memory = "1Gi" }, tools = local.tools, opa = local.opa }
  }
  env = merge({
    PROFILE                 = var.profile
    KAFKA_BOOTSTRAP_SERVERS = var.kafka_bootstrap
    KAFKA_SECURITY_PROTOCOL = "SASL_SSL"
    KAFKA_SASL_MECHANISM    = "OAUTHBEARER"
    KAFKA_OAUTH_PROVIDER    = "azure"
    KAFKA_CA_FILE           = "/etc/ssl/certs/ca-certificates.crt"
    OLLAMA_HOST             = var.ollama_host
    AUDIT_CHECKPOINT_DIR    = "/mnt/state/audit-checkpoints"
    GOVERNED_HEALTH_DIR     = "/mnt/state/health" # shared: /health in the API sees the workers
    TOOL_SERVERS            = join(",", var.tool_servers)
  }, var.app_env)
  secrets = { AUDIT_HMAC_KEY = "audit-hmac-key", OPA_TOKEN = "opa-token" }
}

resource "azurerm_container_app" "svc" {
  for_each                     = local.apps
  name                         = "${var.name}-${each.key}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_ids[each.key]]
  }

  registry {
    server   = var.registry
    identity = var.identity_ids[each.key]
  }

  dynamic "secret" {
    for_each = local.secrets
    content {
      name                = secret.value
      key_vault_secret_id = var.secret_ids[secret.value]
      identity            = var.identity_ids[each.key]
    }
  }

  dynamic "ingress" {
    for_each = each.key == "api" ? [1] : []
    content {
      external_enabled = true # "external" to the environment; the environment itself is internal
      target_port      = 8000
      transport        = "http"
      traffic_weight {
        latest_revision = true
        percentage      = 100
      }
    }
  }

  template {
    min_replicas = 1
    max_replicas = 1

    volume {
      name         = "state"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.state.name
    }

    dynamic "container" {
      for_each = each.value
      content {
        name    = container.key
        image   = "${var.registry}/governed-agentic-ai:${var.image_tag}"
        command = container.value.cmd
        cpu     = container.value.cpu
        memory  = container.value.memory

        dynamic "env" {
          for_each = merge(local.env, { AZURE_CLIENT_ID = var.client_ids[each.key] })
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.secrets
          content {
            name        = env.key
            secret_name = env.value
          }
        }
        volume_mounts {
          name = "state"
          path = "/mnt/state"
        }
      }
    }
  }
}
