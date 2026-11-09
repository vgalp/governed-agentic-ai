package system.authz_test

import data.system.authz

test_the_token_is_required_when_set if {
	authz.allow with input as {"identity": "s3cret"} with opa.runtime as {"env": {"OPA_TOKEN": "s3cret"}}
	not authz.allow with input as {"identity": "guess"} with opa.runtime as {"env": {"OPA_TOKEN": "s3cret"}}
	not authz.allow with input as {} with opa.runtime as {"env": {"OPA_TOKEN": "s3cret"}}
}

test_everything_is_allowed_without_a_token if {
	authz.allow with input as {} with opa.runtime as {"env": {}}
}
