# The GPU VM for Ollama, without a public IP.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//azure/ollama"
}

dependency "network" {
  config_path                             = "${get_terragrunt_dir()}/../network"
  mock_outputs                            = local.mocks.network
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

inputs = {
  resource_group_name  = dependency.network.outputs.resource_group_name
  subnet_id            = dependency.network.outputs.subnet_ids.ollama
  vm_size              = local.env.ollama.vm_size
  models               = local.env.ollama.models
  ollama_version       = try(local.env.ollama.version, "")
  admin_ssh_public_key = local.env.admin_ssh_public_key
}
