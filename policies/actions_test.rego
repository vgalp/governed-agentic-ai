package actions_test

# Run with:  opa test policies/ -v

import data.actions

clinic := {"clinic": {"actions": {"reschedule_appointment": {
	"propose_roles": ["front_desk", "nurse", "clinician"],
	"approve_roles": ["clinician", "nurse"],
	"allowed_times": {"field": "new_start", "weekdays": [0, 1, 2, 3, 4], "start_hour": 8, "end_hour": 17},
}}}}

ask(role, facts) := {"profile": "clinic", "action": "reschedule_appointment", "role": role, "facts": facts}

friday_10 := {"new_start": {"weekday": 4, "hour": 10, "minute": 0, "in_past": false}}

approval(overrides) := object.union(
	{
		"profile": "clinic", "action": "reschedule_appointment", "decision": "approve",
		"requester": "ui", "approver": "dashboard", "approver_role": "nurse", "expired": false,
	},
	overrides,
)

test_front_desk_may_ask_and_it_needs_approval if {
	d := actions.decision with input as ask("front_desk", friday_10) with data.profiles as clinic
	d.allowed
	d.needs_approval
	d.approve_roles == ["clinician", "nurse"]
}

test_role_check_works_before_there_are_facts if {
	actions.decision.allowed with input as ask("nurse", {}) with data.profiles as clinic
}

test_compliance_may_not_ask if {
	d := actions.decision with input as ask("compliance", friday_10) with data.profiles as clinic
	not d.allowed
	d.reasons == ["role compliance may not request this change"]
}

test_unknown_action_is_denied if {
	d := actions.decision with input as {"profile": "clinic", "action": "delete_patient", "role": "nurse"}
		with data.profiles as clinic
	not d.allowed
}

test_weekend_is_denied if {
	facts := {"new_start": {"weekday": 5, "hour": 10, "minute": 0, "in_past": false}}
	d := actions.decision with input as ask("nurse", facts) with data.profiles as clinic
	d.reasons == ["the proposed time is on a day that is not allowed"]
}

test_after_hours_is_denied if {
	facts := {"new_start": {"weekday": 2, "hour": 17, "minute": 30, "in_past": false}}
	d := actions.decision with input as ask("nurse", facts) with data.profiles as clinic
	d.reasons == ["the proposed time is outside the allowed hours"]
}

test_past_time_is_denied if {
	facts := {"new_start": {"weekday": 2, "hour": 10, "minute": 0, "in_past": true}}
	not actions.decision.allowed with input as ask("nurse", facts) with data.profiles as clinic
}

test_approved_by_an_approver_may_run if {
	actions.execute.allowed with input as approval({}) with data.profiles as clinic
}

test_rejected_may_not_run if {
	not actions.execute.allowed with input as approval({"decision": "reject"}) with data.profiles as clinic
}

test_front_desk_may_not_approve if {
	e := actions.execute with input as approval({"approver_role": "front_desk"}) with data.profiles as clinic
	e.reasons == ["role front_desk may not approve this change"]
}

test_nobody_approves_their_own_request if {
	e := actions.execute with input as approval({"approver": "ui"}) with data.profiles as clinic
	e.reasons == ["the person who asked may not approve their own request"]
}

test_expired_may_not_run if {
	not actions.execute.allowed with input as approval({"expired": true}) with data.profiles as clinic
}

test_missing_approver_may_not_run if {
	not actions.execute.allowed with input as object.remove(approval({}), ["approver"]) with data.profiles as clinic
}
