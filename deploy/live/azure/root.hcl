# Shared by every Azure unit (deploy/live/azure/<environment>/<unit>): remote state, the provider,
# names and tags, and the Kafka access list. The environment itself is described in <environment>/env.hcl.

locals {
  env  = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  name = "governed-${local.env.environment}"
  tags = merge({
    project     = "governed-agentic-ai"
    environment = local.env.environment
    managed_by  = "terragrunt"
  }, try(local.env.tags, {}))

  # ops/kafka_access.yaml, the one list of who may use which topic (ADR 031), with its YAML anchors
  # flattened, so the modules get a plain, typed object.
  raw = yamldecode(file("${get_parent_terragrunt_dir()}/../../../ops/kafka_access.yaml"))
  kafka_access = {
    topics = { for t, spec in local.raw.topics : t => { keep = try(spec.keep, "default") } }
    services = { for s, spec in local.raw.services : s => {
      write  = try(flatten(spec.write), [])
      read   = try(flatten(spec.read), [])
      groups = try(flatten(spec.groups), [])
    } }
  }

  # GOVERNED_TG_LOCAL_STATE=1 keeps state on this machine: for trying the configuration without the
  # state storage account (validate, a first plan). Never for a real environment.
  local_state = get_env("GOVERNED_TG_LOCAL_STATE", "") == "1"
  backend = local.local_state ? {
    backend = "local"
    config  = jsonencode({ path = "${get_parent_terragrunt_dir()}/.local-state/${path_relative_to_include()}/terraform.tfstate" })
    } : {
    backend = "azurerm"
    config = jsonencode({
      subscription_id      = try(local.env.state.subscription_id, local.env.subscription_id)
      resource_group_name  = local.env.state.resource_group_name
      storage_account_name = local.env.state.storage_account_name
      container_name       = "tfstate"
      key                  = "${path_relative_to_include()}/terraform.tfstate"
      use_azuread_auth     = true # state access by Entra ID role, not storage keys
    })
  }
}

# State: one blob per unit, in a storage account made once for this purpose (deploy/README.md).
remote_state {
  backend = local.backend.backend
  config  = jsondecode(local.backend.config)
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOT
    provider "azurerm" {
      subscription_id     = "${local.env.subscription_id}"
      storage_use_azuread = true # storage data plane with Entra ID, not account keys
      features {
        key_vault {
          purge_soft_delete_on_destroy = false # the audit key must survive a mistaken destroy
        }
      }
    }
  EOT
}

# Every unit gets these; a module uses the ones it declares.
inputs = {
  name         = local.name
  location     = local.env.location
  unique       = local.env.unique
  tags         = local.tags
  kafka_access = local.kafka_access
}
