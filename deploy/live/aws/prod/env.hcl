# The prod environment on AWS: its own account, network range, sizes and stricter settings.
locals {
  environment = "prod"
  account_id  = "222222222222" # your prod account (not dev's)
  region      = "us-east-1"
  vpc_cidr    = "10.61.0.0/16"
  az_count    = 3 # 3 brokers in 3 zones: 3 copies of each topic, writes survive one zone

  state = {
    bucket = "governed-tfstate-222222222222"
    region = "us-east-1"
  }

  internet_egress = true
  client_cidrs    = []
  certificate_arn = "" # set before people sign in

  kafka = {
    instance_type  = "kafka.m7g.large"
    storage_gb     = 200
    storage_max_gb = 4000
    partitions     = 3
  }
  ollama = {
    instance_type = "g5.2xlarge"
    models        = ["llama-guard3:8b", "mistral"]
  }

  profile            = "clinic-assistant"
  image_tag          = "0.1.0" # promoted from dev: the same image, by tag
  app_env            = {}
  log_retention_days = 90

  tags = { owner = "platform", data = "synthetic-only-until-approved" }
}
