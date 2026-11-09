variable "name" {
  description = "Prefix for every resource name: <project>-<environment>."
  type        = string
}

variable "location" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "subnet_id" {
  description = "The ollama subnet (network module)."
  type        = string
}

variable "vm_size" {
  type    = string
  default = "Standard_NC8as_T4_v3"
}

variable "disk_gb" {
  type    = number
  default = 256
}

variable "ollama_version" {
  description = "Ollama version to install (empty: the newest)."
  type        = string
  default     = ""
}

variable "models" {
  description = "Models to pull: Llama Guard plus the profile's local models."
  type        = list(string)
  default     = ["llama-guard3:8b", "mistral"]
}

variable "admin_ssh_public_key" {
  description = "SSH key of the admin user (reachable only from inside the network)."
  type        = string
}
