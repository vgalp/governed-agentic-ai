variable "name" {
  description = "Prefix for every resource name: <project>-<environment>."
  type        = string
}

variable "kms_key_arn" {
  description = "The environment's key (kms module): everything stored is encrypted with it."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "One private subnet per broker (network module)."
  type        = list(string)
}

variable "apps_security_group_id" {
  type = string
}

variable "kafka_access" {
  description = "ops/kafka_access.yaml, normalized by root.hcl: topics and each service's write, read and groups."
  type = object({
    topics = map(object({ keep = string }))
    services = map(object({
      write  = list(string)
      read   = list(string)
      groups = list(string)
    }))
  })
}

variable "services" {
  description = "The services that run here and get a Kafka policy."
  type        = list(string)
  default     = ["api", "planner", "executor"]
}

variable "kafka_version" {
  type    = string
  default = "3.9.x"
}

variable "instance_type" {
  type    = string
  default = "kafka.m7g.large"
}

variable "storage_gb" {
  description = "Starting disk per broker; grows by itself up to storage_max_gb (the audit topics are kept for good)."
  type        = number
  default     = 100
}

variable "storage_max_gb" {
  type    = number
  default = 2000
}

variable "partitions" {
  type    = number
  default = 3
}
