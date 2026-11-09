# The services on ECS Fargate behind an internal load balancer. Apply once the image is in ECR.
locals {
  env   = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals
  mocks = read_terragrunt_config("${dirname(find_in_parent_folders("root.hcl"))}/_common/mocks.hcl").locals
}

terraform {
  source = "${dirname(find_in_parent_folders("root.hcl"))}/../../modules//aws/apps"
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
  kms_key_arn            = dependency.kms.outputs.key_arn
  vpc_id                 = dependency.network.outputs.vpc_id
  vpc_cidr               = dependency.network.outputs.vpc_cidr
  subnet_ids             = dependency.network.outputs.private_subnet_ids
  apps_security_group_id = dependency.network.outputs.apps_security_group_id
  kafka_bootstrap        = dependency.kafka.outputs.bootstrap
  kafka_policies         = dependency.kafka.outputs.policies
  kafka_setup_policy     = dependency.kafka.outputs.setup_policy
  replication            = dependency.kafka.outputs.replication
  partitions             = local.env.kafka.partitions
  secret_arns            = dependency.secrets.outputs.secret_arns
  registry               = dependency.registry.outputs.url
  efs_file_system_id     = dependency.storage.outputs.file_system_id
  efs_file_system_arn    = dependency.storage.outputs.file_system_arn
  efs_access_point_id    = dependency.storage.outputs.access_point_id
  efs_access_point_arn   = dependency.storage.outputs.access_point_arn
  ollama_host            = dependency.ollama.outputs.host
  client_cidrs           = local.env.client_cidrs
  certificate_arn        = local.env.certificate_arn
  image_tag              = local.env.image_tag
  profile                = local.env.profile
  tool_servers           = try(local.env.tool_servers, null)
  app_env                = try(local.env.app_env, {})
}
