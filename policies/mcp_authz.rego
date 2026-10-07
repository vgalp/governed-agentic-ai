package mcp.authz

# Deny everything unless a rule below allows it.
default allow := false

# A tool call is allowed only if the profile grants the tool to the agent and,
# in a profile with roles, the requesting role may use it. Tool permissions
# come from the active profile, loaded by the gateway into data.profiles.<name>.
# An unknown profile, agent, tool or role is denied.
allow if {
	input.tool in data.profiles[input.profile].agents[input.agent].tools
	role_may_use_tool
}

# Profiles without roles (e.g. the ADHD assistant): the agent's grant is enough.
role_may_use_tool if not data.profiles[input.profile].roles

role_may_use_tool if input.tool in data.profiles[input.profile].roles[input.role].tools