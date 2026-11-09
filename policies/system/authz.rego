package system.authz

# Who may use OPA's REST API (audit SC-5, ADR 030). OPA runs with --authentication=token and
# --authorization=basic, so every request carries "Authorization: Bearer <token>" and this policy
# decides. With OPA_TOKEN set in OPA's environment, only that token is accepted: nothing else on the
# machine can ask for decisions, read the profiles or replace a policy. Without OPA_TOKEN, every
# request is allowed, as before (a first start, before scripts/dev.sh has made the token).

default allow := false

allow if not token_required

allow if {
	token_required
	input.identity == opa.runtime().env.OPA_TOKEN
}

token_required if opa.runtime().env.OPA_TOKEN != ""
