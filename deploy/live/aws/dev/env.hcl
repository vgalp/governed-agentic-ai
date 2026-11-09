# The dev environment on AWS. Everything that differs between environments is here; the units
# (network, kafka, ...) are the same in every environment.
locals {
  environment = "dev"
  account_id  = "111111111111" # your dev account: the provider refuses any other
  region      = "us-east-1"
  vpc_cidr    = "10.60.0.0/16" # different in every environment, so they can be peered later
  az_count    = 2              # 2 brokers: 2 copies of each topic. Prod uses 3

  # Terraform state: a versioned, encrypted bucket made once for this purpose (deploy/README.md).
  state = {
    bucket = "governed-tfstate-111111111111"
    region = "us-east-1"
  }

  internet_egress = true # Ollama install and models, web research, FDA labels; outbound only
  client_cidrs    = []   # who may open the web app (default: the VPC); add VPN ranges
  certificate_arn = ""   # HTTPS on the internal load balancer; sign-in needs it

  kafka = {
    instance_type  = "kafka.t3.small"
    storage_gb     = 50
    storage_max_gb = 500
    partitions     = 1
  }
  ollama = {
    instance_type = "g5.xlarge"
    models        = ["llama-guard3:8b", "mistral"]
  }

  profile            = "clinic-assistant"
  image_tag          = "0.1.0"
  app_env            = {}
  log_retention_days = 14

  tags = { owner = "platform" }
}
