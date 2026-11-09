# The dev environment on Azure. Everything that differs between environments is here; the units
# (network, kafka, ...) are the same in every environment.
locals {
  environment     = "dev"
  subscription_id = "00000000-0000-0000-0000-000000000000" # your dev subscription
  location        = "eastus2"
  unique          = "dv7k2"        # makes the global names unique (registry, vault, storage): 3-6 letters/digits
  address_space   = "10.50.0.0/16" # different in every environment, so they can be peered later

  # Terraform state: a storage account made once for this purpose (deploy/README.md, "State").
  state = {
    resource_group_name  = "governed-tfstate-rg"
    storage_account_name = "governedtfstate"
  }

  internet_egress = true # Ollama install and models, web research, FDA labels; outbound only
  operator_cidrs  = []   # public CIDRs allowed to the registry and vault while deploying

  kafka = {
    capacity   = 1
    partitions = 1
  }
  ollama = {
    vm_size = "Standard_NC4as_T4_v3"
    models  = ["llama-guard3:8b", "mistral"]
  }
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReplaceWithYourOwnKey you@example.org"

  profile   = "clinic-assistant"
  image_tag = "0.1.0"
  app_env   = {} # sign-in settings and the like, no secrets

  tags = { owner = "platform" }
}
