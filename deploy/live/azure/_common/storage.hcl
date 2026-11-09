# Azure Files for the services: audit checkpoints and worker health.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//azure/storage"
}

dependency "network" {
  config_path                             = "${get_terragrunt_dir()}/../network"
  mock_outputs                            = local.mocks.network
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

inputs = {
  resource_group_name = dependency.network.outputs.resource_group_name
  endpoint_subnet_id  = dependency.network.outputs.subnet_ids.endpoints
  dns_zone_ids        = dependency.network.outputs.dns_zone_ids
}
