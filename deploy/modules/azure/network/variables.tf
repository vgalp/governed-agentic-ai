variable "name" {
  description = "Prefix for every resource name: <project>-<environment>."
  type        = string
}

variable "location" {
  type = string
}

variable "address_space" {
  description = "The virtual network's range; split into /23 subnets for apps, private endpoints and Ollama."
  type        = string
}

variable "internet_egress" {
  description = "Outbound internet through a NAT gateway (no inbound). Without it, nothing reaches the internet."
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
