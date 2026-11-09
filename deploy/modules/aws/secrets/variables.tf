variable "name" {
  description = "Prefix for every resource name: <project>-<environment>."
  type        = string
}

variable "kms_key_arn" {

  description = "The environment's key (kms module)."

  type = string

}
