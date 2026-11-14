package roles

# Which data classes a role may see, e.g. ["patient_details", "appointments"].
# The records server builds records from these classes only, and the gateway drops
# any record containing another class. Unknown profile or role: nothing.

default allowed_classes := []

allowed_classes := data.profiles[input.profile].roles[input.role].data_classes if {
	data.profiles[input.profile].roles[input.role]
}

# Which sensitive categories of health information a role may see (ADR 038), e.g.
# ["mental_health", "hiv"]. The gateway withholds any record in another tracked category.
# A role that lists none sees none. Unknown profile or role: none.

default allowed_sensitive := []

allowed_sensitive := data.profiles[input.profile].roles[input.role].sensitive_categories if {
	data.profiles[input.profile].roles[input.role].sensitive_categories
}
