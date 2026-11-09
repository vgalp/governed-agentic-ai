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

variable "subnet_ids" {
  type = list(string)
}

variable "apps_security_group_id" {
  type = string
}
