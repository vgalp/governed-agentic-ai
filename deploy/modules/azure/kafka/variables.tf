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

variable "unique" {
  description = "Lower-case letters and digits that make the globally unique names unique (from env.hcl)."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9]{3,6}$", var.unique))
    error_message = "3 to 6 lower-case letters and digits."
  }
}

variable "endpoint_subnet_id" {
  description = "The private-endpoints subnet (network module)."
  type        = string
}

variable "dns_zone_ids" {
  description = "Private DNS zone IDs by name (network module)."
  type        = map(string)
}

variable "principal_ids" {
  description = "Managed identity principal IDs by service (identities module)."
  type        = map(string)
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
  description = "The services that run here and get Event Hubs roles (the rest of kafka_access is ignored)."
  type        = list(string)
  default     = ["api", "planner", "executor"]
}

variable "capacity" {
  description = "Event Hubs Premium processing units (1, 2, 4, 8 or 16)."
  type        = number
  default     = 1
}

variable "partitions" {
  type    = number
  default = 3
}

variable "archive_replication" {
  description = "Replication of the audit archive (ZRS, or GZRS for a second region)."
  type        = string
  default     = "ZRS"
}

variable "audit_immutability_days" {
  type    = number
  default = 2557 # seven years
}
