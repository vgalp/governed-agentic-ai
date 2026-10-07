# 005: Role-based data access

Status: Accepted · Date: 2026-10-07

## Context
Clinic staff need different parts of a patient's record: front desk needs appointments,
billing needs claims, nurses need medications and visit details. Giving everyone the whole
record and asking the model not to repeat parts of it is not a control: a reworded question,
or a model mistake, can expose what the role should not see.

## Decision
- A profile may define `roles.yaml`: data classes (e.g. `medications`, `visit_clinical`),
  and for each role the tools it may use and the data classes it may see.
- OPA decides. `policies/mcp_authz.rego` allows a tool call only if the agent is granted
  the tool and the role may use it. `policies/roles.rego` returns the role's data classes.
  Unknown profiles, agents, tools and roles are denied or see nothing.
- The records server builds records **from allowed classes only**: data a role may not see
  is never read from the database. Each record lists the classes it contains.
- The gateway checks again and drops any record containing another class, or no class at
  all (fail closed). The count is audited as `dropped_by_gateway`; it should always be 0.
- The model is told which classes the role may not see, so it can say so instead of guessing.
- The role is checked after the input guardrails, so a crisis message gets the crisis reply
  even without a role. A missing or unknown role gets a fixed reply and nothing else.
- Every audit event records the role.

## Consequences
- What the model cannot see it cannot leak: protection does not depend on how a question is
  worded or on the model following instructions.
- Profiles without roles (the ADHD assistant) behave as before.
- In this demonstration the role is chosen in the chat page. A real deployment must take the
  role from the organization's sign-in system (for example SSO claims), never from the user.