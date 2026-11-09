# MSK with IAM sign-in, and each service's Kafka policy from ops/kafka_access.yaml.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//aws/kafka"
}

dependency "kms" {
  config_path                             = "${get_terragrunt_dir()}/../kms"
  mock_outputs                            = local.mocks.kms
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

dependency "network" {
  config_path                             = "${get_terragrunt_dir()}/../network"
  mock_outputs                            = local.mocks.network
  mock_outputs_allowed_terraform_commands = local.mocks.commands
}

inputs = {
  kms_key_arn            = dependency.kms.outputs.key_arn
  vpc_id                 = dependency.network.outputs.vpc_id
  subnet_ids             = dependency.network.outputs.private_subnet_ids
  apps_security_group_id = dependency.network.outputs.apps_security_group_id
  kafka_version          = try(local.env.kafka.version, "3.9.x")
  instance_type          = local.env.kafka.instance_type
  storage_gb             = local.env.kafka.storage_gb
  storage_max_gb         = local.env.kafka.storage_max_gb
  partitions             = local.env.kafka.partitions
}
