# The services in the internal Container Apps environment. Apply once the image is in the registry.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//azure/apps"
}

dependency "network" {
  config_path                             = "${get_terragrunt_dir()}/../network"
  mock_outputs                            = local.mocks.network
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "identities" {
  config_path                             = "${get_terragrunt_dir()}/../identities"
  mock_outputs                            = local.mocks.identities
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "kafka" {
  config_path                             = "${get_terragrunt_dir()}/../kafka"
  mock_outputs                            = local.mocks.kafka
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "secrets" {
  config_path                             = "${get_terragrunt_dir()}/../secrets"
  mock_outputs                            = local.mocks.secrets
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "registry" {
  config_path                             = "${get_terragrunt_dir()}/../registry"
  mock_outputs                            = local.mocks.registry
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "storage" {
  config_path                             = "${get_terragrunt_dir()}/../storage"
  mock_outputs                            = local.mocks.storage
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "ollama" {
  config_path                             = "${get_terragrunt_dir()}/../ollama"
  mock_outputs                            = local.mocks.ollama
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

inputs = {
  resource_group_name = dependency.network.outputs.resource_group_name
  vnet_id             = dependency.network.outputs.vnet_id
  apps_subnet_id      = dependency.network.outputs.subnet_ids.apps
  identity_ids        = dependency.identities.outputs.ids
  client_ids          = dependency.identities.outputs.client_ids
  kafka_bootstrap     = dependency.kafka.outputs.bootstrap
  secret_ids          = dependency.secrets.outputs.secret_ids
  registry            = dependency.registry.outputs.login_server
  state_account_name  = dependency.storage.outputs.account_name
  state_share_name    = dependency.storage.outputs.share_name
  state_access_key    = dependency.storage.outputs.access_key
  ollama_host         = dependency.ollama.outputs.host
  image_tag           = local.env.image_tag
  profile             = local.env.profile
  tool_servers        = try(local.env.tool_servers, null)
  app_env             = try(local.env.app_env, {})
}
