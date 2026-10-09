package roles_test

# Run with:  opa test policies/ -v

import data.mcp.authz
import data.roles

clinic := {"clinic": {
	"agents": {"planner": {"tools": ["search_knowledge", "search_records"]}},
	"tools": {"search_knowledge": {"kind": "read"}, "search_records": {"kind": "read"}},
	"roles": {
		"nurse": {"tools": ["search_knowledge", "search_records"], "data_classes": ["patient_details", "medications"]},
		"compliance": {"tools": ["search_knowledge"], "data_classes": []},
	},
}}

no_roles := {"adhd": {
	"agents": {"planner": {"tools": ["search_knowledge"]}},
	"tools": {"search_knowledge": {"kind": "read"}},
}}

call(profile, role, tool) := {"profile": profile, "agent": "planner", "role": role, "tool": tool}

test_role_sees_only_its_classes if {
	roles.allowed_classes == ["patient_details", "medications"] with input as {"profile": "clinic", "role": "nurse"}
		with data.profiles as clinic
}

test_unknown_role_sees_nothing if {
	roles.allowed_classes == [] with input as {"profile": "clinic", "role": "visitor"} with data.profiles as clinic
}

test_missing_role_sees_nothing if {
	roles.allowed_classes == [] with input as {"profile": "clinic"} with data.profiles as clinic
}

test_role_may_use_granted_tool if {
	authz.allow with input as call("clinic", "nurse", "search_records") with data.profiles as clinic
}

test_role_may_not_use_tool_it_lacks if {
	not authz.allow with input as call("clinic", "compliance", "search_records") with data.profiles as clinic
}

test_unknown_role_may_not_use_tools if {
	not authz.allow with input as call("clinic", "visitor", "search_knowledge") with data.profiles as clinic
}

test_profile_without_roles_uses_agent_grant if {
	authz.allow with input as call("adhd", null, "search_knowledge") with data.profiles as no_roles
}

test_agent_grant_still_required if {
	not authz.allow with input as call("adhd", null, "search_records") with data.profiles as no_roles
}
