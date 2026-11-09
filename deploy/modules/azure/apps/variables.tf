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

variable "vnet_id" {
  type = string
}

variable "apps_subnet_id" {
  type = string
}

variable "identity_ids" {
  description = "Managed identity resource IDs by service (identities module)."
  type        = map(string)
}

variable "client_ids" {
  description = "Managed identity client IDs by service (identities module), for AZURE_CLIENT_ID."
  type        = map(string)
}

variable "registry" {
  description = "The registry's login server (registry module)."
  type        = string
}

variable "image_tag" {
  description = "The image tag to run (never latest: deployments must be repeatable)."
  type        = string
}

variable "secret_ids" {
  description = "Key Vault secret IDs: audit-hmac-key, opa-token (secrets module)."
  type        = map(string)
}

variable "kafka_bootstrap" {
  type = string
}

variable "ollama_host" {
  type = string
}

variable "state_account_name" {
  type = string
}

variable "state_share_name" {
  type = string
}

variable "state_access_key" {
  type      = string
  sensitive = true
}

variable "profile" {
  type = string
}

variable "tool_servers" {
  description = "Tool servers next to the planner and the executor (ops/run_all.py names); null: all."
  type        = list(string)
  nullable    = false
  default     = ["knowledge-base", "records", "changes", "medication-review", "fhir-records", "web-research", "chart-brief", "drug-reference"]
}

variable "app_env" {
  description = "Extra environment for every service, for example the sign-in settings (no secrets)."
  type        = map(string)
  default     = {}
  nullable    = false
}

variable "log_retention_days" {
  type    = number
  default = 30
}
