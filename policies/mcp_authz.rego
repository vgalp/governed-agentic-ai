package mcp.authz

# Deny everything unless a rule below allows it.
default allow := false

# Which tools each agent may call.
allowed_tools := {
	"planner": {"search_knowledge"},
}

allow if {
	input.tool in allowed_tools[input.agent]
}