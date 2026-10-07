package routing

# Decide which model answers a request: the profile's local model or its external one.
#
# Input (facts only, never personal data):
#   profile               profile name
#   entity_types          kinds of personal information found and masked, e.g. ["PERSON"]
#   context_labels        labels of the data tools returned, e.g. ["approved_content"]
#   external_available    an external model is configured and has its credentials
#
# Settings come from the profile (routing section), loaded by the gateway into
# data.profiles.<name>.routing. Anything missing or unknown means local.
#
# The router enforces two rules in code as well, whatever this policy returns:
# external models only ever receive masked text, and a profile with
# external_allowed: false never uses one.

r := data.profiles[input.profile].routing

# Every reason to keep the request local. External only if there are none.
local_reasons contains "no routing settings for this profile" if not r

local_reasons contains "profile does not allow external models" if not r.external_allowed == true

local_reasons contains "no external model available" if not input.external_available == true

local_reasons contains sprintf("contains %s, which never leaves this machine", [e]) if {
	some e in input.entity_types
	e in r.never_external_entities
}

local_reasons contains sprintf("uses %s data, which never leaves this machine", [l]) if {
	some l in input.context_labels
	l in r.never_external_labels
}

local_reasons contains "contains personal information" if {
	r.send_external_when == "no_personal_info"
	count(input.entity_types) > 0
}

local_reasons contains "unknown send_external_when setting" if {
	r
	not r.send_external_when in {"no_personal_info", "masked"}
}

default decision := {"target": "local", "reasons": ["default: local model"]}

decision := {"target": "local", "reasons": sort(local_reasons)} if count(local_reasons) > 0

decision := {"target": "external", "reasons": [reason]} if {
	count(local_reasons) == 0
	reason := external_reason
}

external_reason := "no personal information found; external model allowed" if {
	count(input.entity_types) == 0
} else := "personal information masked; profile allows masked text to go to the external model"
