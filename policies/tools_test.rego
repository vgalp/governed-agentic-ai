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

# Tenant-scoped tools: several customers' data behind one tool. The caller's tenant must
# be known (it comes from the outside endpoint's auth check), or the call is denied.
platform := {"platform": {
	"agents": {"outside": {"tools": ["finance_summary", "policy_search"]}},
	"tools": {
		"finance_summary": {"kind": "read", "tenant_scoped": true},
		"policy_search": {"kind": "read"},
	},
	"roles": {"finance_analyst": {"tools": ["finance_summary", "policy_search"]}},
}}

outside_call(tool, tenant) := {"profile": "platform", "agent": "outside", "role": "finance_analyst",
	"tool": tool, "tenant": tenant}

test_tenant_scoped_tool_allowed_with_a_tenant if {
	authz.allow with input as outside_call("finance_summary", "agency-a") with data.profiles as platform
}

test_tenant_scoped_tool_denied_without_a_tenant if {
	not authz.allow with input as outside_call("finance_summary", "") with data.profiles as platform
}

test_tenant_scoped_tool_denied_when_tenant_missing if {
	no_tenant := {"profile": "platform", "agent": "outside", "role": "finance_analyst", "tool": "finance_summary"}
	not authz.allow with input as no_tenant with data.profiles as platform
}

test_ordinary_tool_needs_no_tenant if {
	authz.allow with input as outside_call("policy_search", "") with data.profiles as platform
}
