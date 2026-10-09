package tools_test

# Run with:  opa test policies/ -v
# The tool registry: a tool must be registered, and a change tool may only be called
# by the agent that carries out approved changes, whatever the agent grants say.

import data.mcp.authz

# Deliberately wrong grants: the planner is given the change tool too. The loader would
# refuse this profile; the policy must still deny it.
clinic := {"clinic": {
	"agents": {
		"planner": {"tools": ["search_records", "reschedule_appointment", "unregistered_tool"]},
		"executor": {"tools": ["reschedule_appointment"]},
	},
	"tools": {
		"search_records": {"kind": "read"},
		"reschedule_appointment": {"kind": "change", "agent": "executor"},
	},
	"roles": {"nurse": {"tools": ["search_records", "reschedule_appointment", "unregistered_tool"]}},
}}

call(agent, tool) := {"profile": "clinic", "agent": agent, "role": "nurse", "tool": tool}

test_read_tool_allowed if {
	authz.allow with input as call("planner", "search_records") with data.profiles as clinic
}

test_executor_may_run_change_tool if {
	authz.allow with input as call("executor", "reschedule_appointment") with data.profiles as clinic
}

test_planner_may_never_run_change_tool if {
	not authz.allow with input as call("planner", "reschedule_appointment") with data.profiles as clinic
}

test_unregistered_tool_denied_even_if_granted if {
	not authz.allow with input as call("planner", "unregistered_tool") with data.profiles as clinic
}

test_change_tool_without_owner_denied if {
	no_owner := json.remove(clinic, ["clinic/tools/reschedule_appointment/agent"])
	not authz.allow with input as call("executor", "reschedule_appointment") with data.profiles as no_owner
}
