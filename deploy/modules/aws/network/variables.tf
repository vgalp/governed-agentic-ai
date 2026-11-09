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

variable "vpc_cidr" {
  type    = string
  default = "10.60.0.0/16"
}

variable "az_count" {
  description = "Availability zones (and Kafka brokers). 3 lets every topic keep 3 copies."
  type        = number
  default     = 3
}

variable "internet_egress" {
  description = "Outbound internet through one NAT gateway (no inbound). Without it, AWS services only, through VPC endpoints."
  type        = bool
  default     = true
}
