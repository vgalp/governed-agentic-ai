package roles

# Which data classes a role may see, e.g. ["patient_details", "appointments"].
# The records server builds records from these classes only, and the gateway drops
# any record containing another class. Unknown profile or role: nothing.

default allowed_classes := []

allowed_classes := data.profiles[input.profile].roles[input.role].data_classes if {
	data.profiles[input.profile].roles[input.role]
}