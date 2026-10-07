# 003: Deployment profiles

Status: Accepted · Date: 2026-10-07

## Context
Everything specific to the ADHD assistant was written into the pipeline code: the system prompt,
guardrail patterns, fixed replies, classifier policy, masking settings, knowledge base and tool
permissions. A second use case (for example a clinic staff assistant) would have meant copying or
branching the code. Organizations adopting the architecture need to configure a use case, review
it, and keep the governed pipeline unchanged.

## Decision
- A **profile** is a folder `profiles/<name>/` holding `profile.yaml` (model, data policy,
  classifier, privacy, knowledge base, agents and their tools), `prompt.md`, `rules.yaml`,
  `responses.yaml`, `classifier_policy.md` and `data/`.
- `profiles/loader.py` loads and validates a profile at startup: required fields, files inside the
  profile folder, valid regexes, known pattern names, and a fixed reply for every category that
  can block a message. A broken profile stops the service before it handles a request.
- The active profile is chosen with `PROFILE=<name>` (default `adhd-assistant`). Pipeline
  functions take an optional profile, so several profiles can later run in one process.
- Tool permissions move from the Rego file into the profile. The gateway loads them into OPA as
  `data.profiles.<name>`; `policies/mcp_authz.rego` allows a call only if the profile grants that
  tool to that agent, and denies unknown profiles, agents and tools.
- Rules are data, not code: named patterns plus ordered `input` and `output` rules with
  `all`/`any` conditions and an optional `sentence` scope.
- The red-team scorer keeps its own dose-amount pattern, so editing a profile cannot change how
  results are scored.

## Consequences
- A new use case is a new folder that can be reviewed like any policy change, with no code change.
- Behaviour did not change: on every prompt and recorded answer in `eval/` (109 prompts,
  333 answers), the profile-driven rules give the same decision, reason and reply as the previous
  hard-coded rules, and the prompt, classifier policy and replies are identical.
- Audit events and responses now record the profile name.
- The profile is trusted configuration: whoever can edit it can change what the system allows.
  It should be reviewed and versioned like code (and, later, signed).
