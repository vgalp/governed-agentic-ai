# The audit key and OPA's token in Secrets Manager.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//aws/secrets"
}

dependency "kms" {
  config_path                             = "${get_terragrunt_dir()}/../kms"
  mock_outputs                            = local.mocks.kms
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

inputs = {
  kms_key_arn = dependency.kms.outputs.key_arn
}
