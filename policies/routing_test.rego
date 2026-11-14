package routing_test

# Run with:  opa test policies/ -v

import data.routing

open_profile := {"p": {"routing": {
	"external_allowed": true,
	"send_external_when": "masked",
	"never_external_entities": ["US_SSN"],
	"never_external_labels": ["patient_record"],
}}}

strict_profile := {"p": {"routing": {
	"external_allowed": true,
	"send_external_when": "no_personal_info",
	"never_external_entities": [],
	"never_external_labels": [],
}}}

closed_profile := {"p": {"routing": {
	"external_allowed": false,
	"send_external_when": "masked",
	"never_external_entities": [],
	"never_external_labels": [],
}}}

req(entities, labels) := {
	"profile": "p",
	"entity_types": entities,
	"context_labels": labels,
	"external_available": true,
}

test_unknown_profile_is_local if {
	routing.decision.target == "local" with input as req([], []) with data.profiles as {}
}

test_closed_profile_is_local if {
	d := routing.decision with input as req([], []) with data.profiles as closed_profile
	d.target == "local"
	"profile does not allow external models" in d.reasons
}

test_no_external_model_is_local if {
	i := object.union(req([], []), {"external_available": false})
	routing.decision.target == "local" with input as i with data.profiles as open_profile
}

test_masked_personal_info_may_go_out_when_allowed if {
	routing.decision.target == "external" with input as req(["PERSON"], []) with data.profiles as open_profile
}

test_blocked_entity_stays_local if {
	d := routing.decision with input as req(["PERSON", "US_SSN"], []) with data.profiles as open_profile
	d.target == "local"
	"contains US_SSN, which never leaves this machine" in d.reasons
}

test_blocked_label_stays_local if {
	d := routing.decision with input as req([], ["patient_record"]) with data.profiles as open_profile
	d.target == "local"
}

test_strict_profile_sends_only_without_personal_info if {
	routing.decision.target == "external" with input as req([], ["approved_content"]) with data.profiles as strict_profile
	routing.decision.target == "local" with input as req(["PERSON"], []) with data.profiles as strict_profile
}

test_missing_input_fields_are_local if {
	routing.decision.target == "local" with input as {"profile": "p"} with data.profiles as open_profile
}

# --- sensitive categories (ADR 038) ---------------------------------------------------------------

guarded_profile := {"p": {"routing": {
	"external_allowed": true,
	"send_external_when": "masked",
	"never_external_entities": [],
	"never_external_labels": [],
	"never_external_categories": ["substance_use", "hiv"],
}}}

about(categories) := object.union(req([], []), {"sensitive_categories": categories})

test_sensitive_category_stays_local if {
	d := routing.decision with input as about(["substance_use"]) with data.profiles as guarded_profile
	d.target == "local"
	"is about substance_use, which never leaves this machine" in d.reasons
}

test_category_the_profile_lets_out_may_go if {
	routing.decision.target == "external" with input as about(["genetic"]) with data.profiles as guarded_profile
}

test_no_sensitive_category_changes_nothing if {
	routing.decision.target == "external" with input as about([]) with data.profiles as guarded_profile
	routing.decision.target == "external" with input as req([], []) with data.profiles as guarded_profile
	# a profile that lists no categories is not affected by a caller that sends some
	routing.decision.target == "external" with input as about(["hiv"]) with data.profiles as open_profile
}
