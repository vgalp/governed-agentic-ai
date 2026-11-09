variable "name" {
  type = string
}

variable "location" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "subnet_id" {
  description = "The private-endpoints subnet."
  type        = string
}

variable "resource_id" {
  type = string
}

variable "subresource" {
  description = "namespace, vault, registry, blob, file ..."
  type        = string
}

variable "dns_zone_id" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
