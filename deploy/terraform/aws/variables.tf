variable "name" {
  description = "Short name used in every resource name."
  type        = string
  default     = "governed"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,15}$", var.name))
    error_message = "3 to 16 lower-case letters, digits and dashes, starting with a letter."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.60.0.0/16"
}

variable "az_count" {
  description = "Availability zones (and Kafka brokers). 3 lets every topic keep 3 copies."
  type        = number
  default     = 3
}

variable "client_cidrs" {
  description = "Networks that may open the web app through the internal load balancer (default: the VPC itself; add your VPN or Direct Connect ranges)."
  type        = list(string)
  default     = []
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS on the internal load balancer. Empty: HTTP on port 80, for a first test only (sign-in needs HTTPS)."
  type        = string
  default     = ""
}

variable "internet_egress" {
  description = <<-EOT
    Outbound internet through a NAT gateway (no inbound). Needed for: installing Ollama and pulling its
    models, the web research and FDA label tools, and a hosted model provider if a profile uses one.
    Without it nothing in the VPC can reach the internet; AWS services are reached through VPC endpoints.
  EOT
  type        = bool
  default     = true
}

variable "profile" {
  type    = string
  default = "clinic-assistant"
}

variable "deploy_apps" {
  description = "false for the first apply (network, Kafka, registry, secrets, Ollama); true once the image is pushed."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the image in ECR (tags are immutable there)."
  type        = string
  default     = "0.1.0"
}

variable "kafka_version" {
  type    = string
  default = "3.9.x"
}

variable "kafka_instance_type" {
  type    = string
  default = "kafka.m7g.large"
}

variable "kafka_storage_gb" {
  description = "Starting disk per broker; grows by itself up to kafka_storage_max_gb (the audit topics are kept for good)."
  type        = number
  default     = 100
}

variable "kafka_storage_max_gb" {
  type    = number
  default = 2000
}

variable "partitions" {
  type    = number
  default = 3
}

variable "ollama_instance_type" {
  type    = string
  default = "g5.xlarge"
}

variable "ollama_version" {
  type    = string
  default = ""
}

variable "ollama_models" {
  type    = list(string)
  default = ["llama-guard3:8b", "mistral"]
}

variable "tool_servers" {
  type    = list(string)
  default = ["knowledge-base", "records", "changes", "medication-review", "fhir-records", "web-research", "chart-brief", "drug-reference"]
}

variable "app_env" {
  description = "Extra environment for every service, for example the sign-in settings (no secrets here)."
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = { project = "governed-agentic-ai" }
}
