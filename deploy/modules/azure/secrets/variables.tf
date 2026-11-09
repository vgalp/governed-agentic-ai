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

variable "operator_cidrs" {
  description = "Public CIDRs that may reach this while deploying; empty: only from inside the network."
  type        = list(string)
  default     = []
}
