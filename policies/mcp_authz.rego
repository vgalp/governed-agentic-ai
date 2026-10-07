package mcp.authz

# Deny everything unless a rule below allows it.
default allow := false

# Which tools each agent may call comes from the active profile
# (profiles/<name>/profile.yaml), loaded by the gateway into data.profiles.<name>.
# An unknown profile, agent or tool is denied.
allow if {
	input.tool in data.profiles[input.profile].agents[input.agent].tools
}
