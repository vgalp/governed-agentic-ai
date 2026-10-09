# 008: Choosing the hosted model provider: OpenAI, Gemini or Claude

Status: Accepted · Date: 2026-10-07

## Context
Organizations want to use capable hosted models for general questions while keeping
private data on their own systems, and many already have an agreement with one provider.
Routing (ADR 004) already decides whether a request may leave at all; until now a profile
could name only one external model, through a generic OpenAI-compatible setting.

## Decision
- Three hosted providers are supported: `openai`, `gemini` (through Google's
  OpenAI-compatible API) and `anthropic` (Claude, through the Messages API). Each needs
  only an address (with a default), a model name and the name of the environment variable
  that holds its API key. No provider libraries are added.
- A profile lists the models it may use. `routing.external_model` picks the one in front;
  `routing.fallback` lists others to try, in order. The local model is always the last
  resort and is never listed.
- The first model in that order whose key is set is used. If a call fails (no connection,
  an HTTP error, no answer text), the next is tried, still with masked text: the policy
  already allowed this data out. Each switch is audited (`audit.routing`, `fallback: true`)
  and shown in the decision trace.
- `EXTERNAL_MODEL=<key>` at startup puts another of the profile's listed models in front,
  for demos and comparisons. It cannot add a model the profile does not list.
- The provider makes no difference to what may leave: the routing policy and the checks in
  code are unchanged. Patient records and personal information stay local whichever
  provider is configured (tested for all three).
- A hosted provider can never be marked `location: local`, since local models may see
  unmasked text; the loader refuses it. Hosted providers must name an API key variable.
- Provider errors report the HTTP status only, never the response body, which may echo
  the request.
- Users never choose the model. The organization's profile does.

## Consequences
- Switching provider is one line in the profile, or one setting at startup.
- The same guardrail evaluation can be run through each provider and the local model,
  to show that the controls behave the same whichever model answers.
- Not yet: different data permissions per provider (for example, allowing internal data
  to go only to a provider the organization has a business associate agreement with).
  Added when a pilot needs it.
- Model names change over time; they are configuration, checked against each provider's
  current list.