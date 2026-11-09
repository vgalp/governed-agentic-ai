# The prod environment on Azure: its own subscription, network range, sizes and stricter settings.
locals {
  environment     = "prod"
  subscription_id = "00000000-0000-0000-0000-000000000000" # your prod subscription (not dev's)
  location        = "eastus2"
  unique          = "pr4m8"
  address_space   = "10.51.0.0/16"

  state = {
    resource_group_name  = "governed-tfstate-rg"
    storage_account_name = "governedtfstate"
  }

  internet_egress = true
  operator_cidrs  = [] # deploy prod from inside the network

  kafka = {
    capacity                = 2
    partitions              = 3
    archive_replication     = "GZRS" # the audit archive also in the paired region
    audit_immutability_days = 2557
  }
  ollama = {
    vm_size = "Standard_NC8as_T4_v3"
    models  = ["llama-guard3:8b", "mistral"]
  }
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReplaceWithYourOwnKey you@example.org"

  profile   = "clinic-assistant"
  image_tag = "0.1.0" # promoted from dev: the same image, by tag
  app_env   = {}

  tags = { owner = "platform", data = "synthetic-only-until-approved" }
}
