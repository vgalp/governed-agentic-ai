variable "name" {
  description = "Prefix for every resource name: <project>-<environment>."
  type        = string
}

variable "kms_key_arn" {

  description = "The environment's key (kms module)."

  type = string

}

variable "vpc_id" {
  type = string
}

variable "subnet_id" {
  description = "A private subnet (network module)."
  type        = string
}

variable "apps_security_group_id" {
  type = string
}

variable "instance_type" {
  type    = string
  default = "g5.xlarge"
}

variable "disk_gb" {
  type    = number
  default = 200
}

variable "ollama_version" {
  description = "Ollama version to install (empty: the newest)."
  type        = string
  default     = ""
}

variable "models" {
  type    = list(string)
  default = ["llama-guard3:8b", "mistral"]
}
