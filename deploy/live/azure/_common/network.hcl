# The virtual network, subnets, security groups, NAT (optional) and private DNS zones.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//azure/network"
}

inputs = {
  address_space   = local.env.address_space
  internet_egress = local.env.internet_egress
}
