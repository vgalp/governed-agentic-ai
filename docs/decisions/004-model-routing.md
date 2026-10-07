# 004: Policy-based model routing

Status: Accepted · Date: 2026-10-07

## Context
Every request went to one local model. Organizations often want a stronger external model for
general questions while keeping personal and health information on their own machines. The choice
of model is a data-protection decision, so it should be made by reviewable policy, recorded, and
impossible to get wrong by a configuration mistake.

## Decision
- A profile lists its **models** (provider, name, `location: local | external`) and **routing**
  settings: whether external models are allowed at all, when (`no_personal_info` or `masked`),
  entity types and data labels that never leave the machine, and whether the local model sees
  masked or original text. API keys are never in a profile: only the name of an environment variable.
- `policies/routing.rego` decides `local` or `external` from **facts only**: the kinds of personal
  information that were masked (e.g. `PERSON`, never the values), the labels of the data tools
  returned (`tool_data_labels`), and whether an external model has its credentials. Every reason
  to stay local is listed; the request goes external only if there are none.
- The router (`router/router.py`) also enforces, in code and whatever OPA returns:
  1. an external model only ever receives masked text;
  2. a profile with `external_allowed: false` never uses an external model;
  3. if OPA cannot be reached, the local model answers (fail safe).
- If an external model fails, the request falls back to the local model; the fallback is audited.
- Every decision is written to the tamper-evident audit chain on `audit.routing`: target, model,
  provider, whether text was masked, entity types, data labels and the policy's reasons.
  `audit.model_inputs` always records the masked text, even when the local model saw the original,
  so the audit never stores raw personal data.
- Providers (`llm/providers.py`): `ollama`, `openai_compatible` (hosted services, or self-hosted
  servers such as vLLM with `location: local`) and `mock` (tests, machines without a GPU).

## Consequences
- The ADHD profile keeps `external_allowed: false`: every request is answered locally with masked
  text, as before.
- Policy tests run with `opa test policies/` in CI; the code-side rules are tested in
  `tests/test_router.py`.
- A wrong policy can keep data local when it could have gone out, but cannot send unmasked text
  out or use an external model the profile forbids.
- Masking quality remains the limit: text the masker misses is not masked. Profiles that handle
  health records should set `never_external_labels` for record data, so it never leaves at all.
