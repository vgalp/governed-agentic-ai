# The VPC: private subnets, VPC endpoints, NAT (optional), flow logs, the services' security group.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//aws/network"
}

dependency "kms" {
  config_path                             = "${get_terragrunt_dir()}/../kms"
  mock_outputs                            = local.mocks.kms
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

inputs = {
  kms_key_arn     = dependency.kms.outputs.key_arn
  vpc_cidr        = local.env.vpc_cidr
  az_count        = local.env.az_count
  internet_egress = local.env.internet_egress
}
