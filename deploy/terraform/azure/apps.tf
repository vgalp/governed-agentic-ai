# The services, in an internal Container Apps environment: its only address is a private IP in the
# apps subnet, and its names resolve only inside the network.
#
#   api       the web app, dashboard and /health (port 8000, reachable from the network)
#   planner   the planner, the tool servers and OPA, sharing 127.0.0.1 as on one machine
#   executor  the executor, the tool servers and OPA
#
# Each app signs in to Kafka, the registry and the vault as its own managed identity. One replica
# each: the executor rebuilds its state from Kafka and must not run twice.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.name}-logs"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30 # service logs hold no message text, but keep them short anyway
  tags                = var.tags
}

resource "azurerm_container_app_environment" "main" {
  name                               = "${var.name}-apps"
  location                           = var.location
  resource_group_name                = azurerm_resource_group.main.name
  log_analytics_workspace_id         = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id           = azurerm_subnet.apps.id
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
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "apps" {
  name                  = "${var.name}-apps"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.apps.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_a_record" "apps" {
  name                = "*"
  zone_name           = azurerm_private_dns_zone.apps.name
  resource_group_name = azurerm_resource_group.main.name
  ttl                 = 300
  records             = [azurerm_container_app_environment.main.static_ip_address]
}

resource "azurerm_container_app_environment_storage" "state" {
  name                         = "state"
  container_app_environment_id = azurerm_container_app_environment.main.id
  account_name                 = azurerm_storage_account.state.name
  share_name                   = azurerm_storage_share.state.name
  access_key                   = azurerm_storage_account.state.primary_access_key
  access_mode                  = "ReadWrite"
}

locals {
  image = "${azurerm_container_registry.main.login_server}/governed-agentic-ai:${var.image_tag}"
  opa = {
    cmd = ["opa", "run", "--server", "--addr", "127.0.0.1:8181", "--authentication=token",
    "--authorization=basic", "/app/policies"]
    cpu = 0.25, memory = "0.5Gi"
  }
  tools = { cmd = ["python", "-m", "ops.run_all"], cpu = 0.5, memory = "1Gi" }
  apps = {
    api = {
      containers = { api = { cmd = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"], cpu = 1.0, memory = "2Gi" } }
      env        = {}
    }
    planner = {
      containers = { planner = { cmd = ["python", "-m", "agents.planner"], cpu = 1.25, memory = "2.5Gi" }, tools = local.tools, opa = local.opa }
      env        = {}
    }
    executor = {
      containers = { executor = { cmd = ["python", "-m", "agents.executor"], cpu = 0.5, memory = "1Gi" }, tools = local.tools, opa = local.opa }
      env        = {}
    }
  }
  common_env = merge({
    PROFILE                 = var.profile
    KAFKA_BOOTSTRAP_SERVERS = local.kafka_bootstrap
    KAFKA_SECURITY_PROTOCOL = "SASL_SSL"
    KAFKA_SASL_MECHANISM    = "OAUTHBEARER"
    KAFKA_OAUTH_PROVIDER    = "azure"
    KAFKA_CA_FILE           = "/etc/ssl/certs/ca-certificates.crt"
    OLLAMA_HOST             = "http://${azurerm_network_interface.ollama.private_ip_address}:11434"
    AUDIT_CHECKPOINT_DIR    = "/mnt/state/audit-checkpoints"
    TOOL_SERVERS            = join(",", var.tool_servers)
  }, var.app_env)
  secrets = { AUDIT_HMAC_KEY = "audit-hmac-key", OPA_TOKEN = "opa-token" }
}

resource "azurerm_container_app" "svc" {
  for_each                     = var.deploy_apps ? local.apps : {}
  name                         = "${var.name}-${each.key}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.svc[each.key].id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.svc[each.key].id
  }

  dynamic "secret" {
    for_each = local.secrets
    content {
      name                = secret.value
      key_vault_secret_id = azurerm_key_vault_secret.secret[secret.value].versionless_id
      identity            = azurerm_user_assigned_identity.svc[each.key].id
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
      for_each = each.value.containers
      content {
        name    = container.key
        image   = local.image
        command = container.value.cmd
        cpu     = container.value.cpu
        memory  = container.value.memory

        dynamic "env" {
          for_each = merge(local.common_env, each.value.env, {
            AZURE_CLIENT_ID     = azurerm_user_assigned_identity.svc[each.key].client_id
            GOVERNED_HEALTH_DIR = "/mnt/state/health" # shared: /health in the API sees the workers
          })
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

  depends_on = [azurerm_role_assignment.svc_pull, azurerm_role_assignment.svc_secrets, azurerm_role_assignment.kafka]
}
