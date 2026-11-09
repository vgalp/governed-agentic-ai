# Placeholder outputs, used only by validate and by a plan before a dependency exists. They have the
# shape of real AWS IDs and ARNs because the provider checks the format.
locals {
  commands = ["init", "validate", "plan"]
  arn      = "arn:aws:kafka:us-east-1:111111111111:cluster/mock/00000000-0000-0000-0000-000000000000-1"
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["kafka-cluster:Connect"], Resource = ["arn:aws:kafka:us-east-1:111111111111:cluster/mock/00000000-0000-0000-0000-000000000000-1"] }] })

  kms = { key_arn = "arn:aws:kms:us-east-1:111111111111:key/00000000-0000-0000-0000-000000000000" }
  network = {
    vpc_id                 = "vpc-00000000000000000"
    vpc_cidr               = "10.0.0.0/16"
    private_subnet_ids     = ["subnet-00000000000000001", "subnet-00000000000000002", "subnet-00000000000000003"]
    apps_security_group_id = "sg-00000000000000000"
  }
  kafka = {
    bootstrap    = "b-1.mock.kafka.us-east-1.amazonaws.com:9098"
    cluster_arn  = local.arn
    replication  = 3
    policies     = { api = local.policy, planner = local.policy, executor = local.policy }
    setup_policy = local.policy
  }
  secrets = { secret_arns = {
    "audit-hmac-key" = "arn:aws:secretsmanager:us-east-1:111111111111:secret:mock/audit-hmac-key-AbCdEf"
    "opa-token"      = "arn:aws:secretsmanager:us-east-1:111111111111:secret:mock/opa-token-AbCdEf"
  } }
  registry = { url = "111111111111.dkr.ecr.us-east-1.amazonaws.com/mock/governed-agentic-ai" }
  storage = {
    file_system_id   = "fs-00000000"
    file_system_arn  = "arn:aws:elasticfilesystem:us-east-1:111111111111:file-system/fs-00000000"
    access_point_id  = "fsap-00000000000000000"
    access_point_arn = "arn:aws:elasticfilesystem:us-east-1:111111111111:access-point/fsap-00000000000000000"
  }
  ollama = { host = "http://10.0.0.10:11434", private_ip = "10.0.0.10" }
}
