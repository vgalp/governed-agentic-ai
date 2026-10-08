package actions

# Changes the assistant may propose (profiles/<name>/actions.yaml), decided in two steps.
# The language model never decides: it only turns a request into a proposal. Facts
# come from code (who asked, the proposed time); settings from the profile, loaded by
# the gateway into data.profiles.<name>.actions. Anything unknown is denied.
#
# 1. decision: may this role ask for this change, with these facts? Every allowed
#    change still needs a person's approval, by one of approve_roles.
# 2. execute: after a person decided, may the change be made now?

a := data.profiles[input.profile].actions[input.action]

# --- 1. proposal ----------------------------------------------------------------

deny_reasons contains "unknown action" if not a

deny_reasons contains sprintf("role %s may not request this change", [input.role]) if {
	a
	not input.role in a.propose_roles
}

# A datetime field limited to certain days and hours (e.g. clinic opening hours).
# Only checked once the proposal has facts; the first check is about the role only.
time_facts := input.facts[a.allowed_times.field]

deny_reasons contains "the new time is in the past" if time_facts.in_past == true

deny_reasons contains "the new time is on a day the clinic is closed" if {
	not time_facts.weekday in a.allowed_times.weekdays
}

deny_reasons contains "the new time is outside opening hours" if {
	time_facts.hour < a.allowed_times.start_hour
}

deny_reasons contains "the new time is outside opening hours" if {
	time_facts.hour >= a.allowed_times.end_hour
}

default decision := {"allowed": false, "reasons": ["default: denied"]}

decision := {"allowed": false, "reasons": sort(deny_reasons)} if count(deny_reasons) > 0

decision := {
	"allowed": true,
	"needs_approval": true,
	"approve_roles": a.approve_roles,
	"reasons": ["allowed by policy; needs approval by a person"],
} if {
	a
	count(deny_reasons) == 0
}

# --- 2. execution ---------------------------------------------------------------

execute_reasons contains "unknown action" if not a

execute_reasons contains "the change was not approved" if input.decision != "approve"

execute_reasons contains "no approver given" if not input.approver

execute_reasons contains sprintf("role %s may not approve this change", [input.approver_role]) if {
	a
	not input.approver_role in a.approve_roles
}

# Separation of duties: nobody approves their own request.
execute_reasons contains "the person who asked may not approve their own request" if {
	input.approver
	input.approver == input.requester
}

execute_reasons contains "the approval request has expired" if input.expired == true

default execute := {"allowed": false, "reasons": ["default: denied"]}

execute := {"allowed": false, "reasons": sort(execute_reasons)} if count(execute_reasons) > 0

execute := {"allowed": true, "reasons": ["approved by a person with an approver role"]} if {
	a
	count(execute_reasons) == 0
}