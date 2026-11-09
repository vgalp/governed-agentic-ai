variable "name" {
  description = "Short name used in every resource name (letters and digits, starts with a letter)."
  type        = string
  default     = "governed"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.name))
    error_message = "3 to 12 lower-case letters and digits, starting with a letter."
  }
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "eastus2"
}

variable "address_space" {
  description = "The virtual network's address range (a /16 is split into the subnets below)."
  type        = string
  default     = "10.50.0.0/16"
}

variable "profile" {
  description = "Which profile every service loads (profiles/<name>)."
  type        = string
  default     = "clinic-assistant"
}

variable "deploy_apps" {
  description = "false for the first apply (network, Kafka, registry, vault, Ollama); true once the image is pushed."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the governed-agentic-ai image in the registry (never latest: deployments must be repeatable)."
  type        = string
  default     = "0.1.0"
}

variable "operator_cidrs" {
  description = <<-EOT
    Public addresses (CIDR) that may reach the registry and the key vault while you deploy, for example
    to push the image and let Terraform write the secrets. Empty (the default) means nothing public can:
    run Terraform and the image push from a machine inside the virtual network (VPN, ExpressRoute, a
    peered network or a build agent there). Remove the entries again when the deployment is done.
  EOT
  type        = list(string)
  default     = []
}

variable "internet_egress" {
  description = <<-EOT
    Outbound internet through a NAT gateway (no inbound). Needed for: installing Ollama and pulling its
    models on the GPU VM, the web research and FDA label tools, and a hosted model provider if a profile
    uses one. Without it nothing in the network can reach the internet at all.
  EOT
  type        = bool
  default     = true
}

variable "eventhubs_capacity" {
  description = "Event Hubs Premium processing units (1, 2, 4, 8 or 16)."
  type        = number
  default     = 1
}

variable "partitions" {
  description = "Partitions per topic (event hub)."
  type        = number
  default     = 3
}

variable "audit_immutability_days" {
  description = "Days each archived audit file cannot be changed or deleted (the policy starts unlocked: see README)."
  type        = number
  default     = 2557 # seven years
}

variable "ollama_vm_size" {
  description = "GPU VM for Ollama (Llama Guard and the profile's local models)."
  type        = string
  default     = "Standard_NC8as_T4_v3"
}

variable "ollama_version" {
  description = "Ollama version to install (empty: the newest)."
  type        = string
  default     = ""
}

variable "ollama_models" {
  description = "Models pulled onto the GPU VM: Llama Guard plus the profile's own (profile.yaml, provider ollama)."
  type        = list(string)
  default     = ["llama-guard3:8b", "mistral"]
}

variable "admin_ssh_public_key" {
  description = "SSH public key for the GPU VM's admin user (reachable only from inside the network)."
  type        = string
}

variable "tool_servers" {
  description = "Tool servers run next to the planner and the executor (ops/run_all.py names)."
  type        = list(string)
  default     = ["knowledge-base", "records", "changes", "medication-review", "fhir-records", "web-research", "chart-brief", "drug-reference"]
}

variable "app_env" {
  description = "Extra environment for every service, for example the sign-in settings (no secrets here)."
  type        = map(string)
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = { project = "governed-agentic-ai" }
}
