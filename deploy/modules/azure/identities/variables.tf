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

variable "services" {
  description = "The services that run in this environment."
  type        = list(string)
  default     = ["api", "planner", "executor"]
}
