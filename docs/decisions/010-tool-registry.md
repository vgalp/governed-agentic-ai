# 010: Tool registry in the profile

Status: Accepted · Date: 2026-10-08

## Context
A profile should be the only thing that changes between deployments (ADR 003). For tools,
that was not yet true. What the platform knew about a tool was spread over five places:
the gateway's list of server addresses (in code), `tool_data_labels` and
`tools_receiving_identifiers` in `profile.yaml`, `filtered_tools` in `roles.yaml`, and the
decision trace's map of tools to lanes (in code). Adding a tool meant editing code, and the
rules that keep tools safe (only the executor may make changes, names only go to tools whose
data stays local) were partly implicit.

## Decision
Each profile has a `tools.yaml` with one entry per tool:

| Field | Meaning |
| --- | --- |
| `server` | where the tool's MCP server listens |
| `kind` | `read` returns data; `change` changes something, only after a person approves |
| `data_label` | what kind of data it returns; routing decides by it (ADR 004) |
| `lane` | where its steps show in the decision trace (`knowledge` or `records`; change tools always `action`) |
| `receives_identifiers` | gets the real question, e.g. a name to look up |
| `role_filtered` | results filtered by the data classes the role may see (ADR 005) |

Which agent may call a tool stays in `profile.yaml` (`agents`), and which role may use it in
`roles.yaml`. The registry says what a tool is; the grants say who may use it.

The loader refuses a profile, at startup, when:

- an agent is granted a tool that is not registered;
- the planner (the agent that talks to the model) is granted a change tool;
- a change tool is granted but no action in `actions.yaml` uses it, so nothing would ask a
  person first, or an action uses a read tool, or names the planner as the agent that acts;
- a tool receives identifiers but its data label may leave the machine;
- a tool is role-filtered in a profile without roles, or a change tool is marked as receiving
  identifiers or role-filtered (it gets validated IDs only);
- an entry is malformed (no server URL, unknown kind or lane), or uses the old settings.

OPA enforces the same rule independently. The gateway loads each tool's kind, and for a change
tool the one agent that may run it, into the policy data; `policies/mcp_authz.rego` allows a
call only to a registered tool, and a change tool only from that agent. This holds even if the
grants were wrong (tested in `policies/tools_test.rego`).

## Consequences
- Adding a tool to a deployment is a profile change: a `tools.yaml` entry plus grants. The code
  that calls tools, routes, filters and traces reads the registry.
- The MCP servers themselves are still started by `scripts/dev.sh`, and their addresses are
  localhost. Reading infrastructure settings from the environment is the next step.
- The planner still calls every read tool it is granted on every request. Choosing tools per
  request is a separate change.
- The old settings (`tool_data_labels`, `tools_receiving_identifiers`, `filtered_tools`) are
  refused with a message pointing to `tools.yaml`, rather than silently ignored.