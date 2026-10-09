package mcp.authz

# Deny everything unless a rule below allows it.
default allow := false

# A tool call is allowed only if the profile grants the tool to the agent, the tool is
# in the profile's tool registry (tools.yaml), the agent may use a tool of that kind,
# and, in a profile with roles, the requesting role may use it. All of this comes from
# the active profile, loaded by the gateway into data.profiles.<name>.
# An unknown profile, agent, tool or role is denied.
allow if {
	input.tool in data.profiles[input.profile].agents[input.agent].tools
	agent_may_use_kind
	role_may_use_tool
}

# A read tool may be used by any agent the profile grants it to.
agent_may_use_kind if data.profiles[input.profile].tools[input.tool].kind == "read"

# A change tool only by the one agent that carries out approved changes. The loader
# already refuses any other grant; this holds even if the policy data were edited.
agent_may_use_kind if {
	tool := data.profiles[input.profile].tools[input.tool]
	tool.kind == "change"
	input.agent == tool.agent
}

# Profiles without roles (e.g. the ADHD assistant): the agent's grant is enough.
role_may_use_tool if not data.profiles[input.profile].roles

role_may_use_tool if input.tool in data.profiles[input.profile].roles[input.role].tools
