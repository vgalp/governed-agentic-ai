variable "name" {
  description = "Prefix for every resource name: <project>-<environment>."
  type        = string
}

variable "kms_key_arn" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for the tasks and the internal load balancer (network module)."
  type        = list(string)
}

variable "apps_security_group_id" {
  type = string
}

variable "client_cidrs" {
  description = "Networks that may open the web app (default: the VPC). Add VPN or Direct Connect ranges."
  type        = list(string)
  default     = []
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS on the load balancer. Empty: HTTP on port 80, for a first test only (sign-in needs HTTPS)."
  type        = string
  default     = ""
}

variable "registry" {
  description = "The ECR repository URL (registry module)."
  type        = string
}

variable "image_tag" {
  description = "The image tag to run (tags are immutable in ECR)."
  type        = string
}

variable "kafka_bootstrap" {
  type = string
}

variable "kafka_policies" {
  description = "Each service's Kafka policy JSON (kafka module)."
  type        = map(string)
}

variable "kafka_setup_policy" {
  type = string
}

variable "replication" {
  type    = number
  default = 3
}

variable "partitions" {
  type    = number
  default = 3
}

variable "secret_arns" {
  description = "audit-hmac-key and opa-token (secrets module)."
  type        = map(string)
}

variable "efs_file_system_id" {
  type = string
}

variable "efs_file_system_arn" {
  type = string
}

variable "efs_access_point_id" {
  type = string
}

variable "efs_access_point_arn" {
  type = string
}

variable "ollama_host" {
  type = string
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
