# Shared by every AWS unit (deploy/live/aws/<environment>/<unit>): remote state, the provider, names
# and tags, and the Kafka access list. The environment itself is described in <environment>/env.hcl.

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
  # state bucket (validate, a first plan). Never for a real environment.
  local_state = get_env("GOVERNED_TG_LOCAL_STATE", "") == "1"
  backend = local.local_state ? {
    backend = "local"
    config  = jsonencode({ path = "${get_parent_terragrunt_dir()}/.local-state/${path_relative_to_include()}/terraform.tfstate" })
    } : {
    backend = "s3"
    config = jsonencode({
      bucket       = local.env.state.bucket
      key          = "${path_relative_to_include()}/terraform.tfstate"
      region       = local.env.state.region
      encrypt      = true
      use_lockfile = true # S3 native locking (OpenTofu / Terraform 1.10+), no DynamoDB table
    })
  }
}

# State: one object per unit, in a versioned, encrypted bucket made once for this purpose.
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
    provider "aws" {
      region              = "${local.env.region}"
      allowed_account_ids = ["${local.env.account_id}"] # dev settings can never be applied to the prod account
      default_tags {
        tags = ${jsonencode(local.tags)}
      }
    }
  EOT
}

# Every unit gets these; a module uses the ones it declares.
inputs = {
  name               = local.name
  kafka_access       = local.kafka_access
  log_retention_days = try(local.env.log_retention_days, 30)
}
