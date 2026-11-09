# Event Hubs (Kafka) with a role per service per hub from ops/kafka_access.yaml, and the audit archive.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//azure/kafka"
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

inputs = {
  resource_group_name     = dependency.network.outputs.resource_group_name
  endpoint_subnet_id      = dependency.network.outputs.subnet_ids.endpoints
  dns_zone_ids            = dependency.network.outputs.dns_zone_ids
  principal_ids           = dependency.identities.outputs.principal_ids
  capacity                = local.env.kafka.capacity
  partitions              = local.env.kafka.partitions
  archive_replication     = try(local.env.kafka.archive_replication, "ZRS")
  audit_immutability_days = try(local.env.kafka.audit_immutability_days, 2557)
}
