# 015. Governed MCP endpoint for outside assistants

Status: Accepted · Date: 2026-10-15

(014 is kept for signing in through SMART on FHIR, as ADR 013 says.)

## Context

Until now only this project's own agents reached tools, through the gateway (ADR 010): OPA decides
which agent and role may call which tool, record data is filtered by role (ADR 005), and every call
is audited (ADR 002). But staff in clinics, agencies and offices increasingly use assistants their
organization buys or builds: Claude, ChatGPT, Copilot, Kiro, or an assistant inside a vendor's app.
These reach tools and data through the Model Context Protocol (MCP). Connected directly to a data
server, they get none of the rules above.

Software platforms add a second need. A platform that serves many customers (tenants) holds all of
their data behind the same tools. An assistant acting for one customer must never see another's.

## Decision

1. **One governed front door.** `gateway/endpoint.py` is an MCP server (streamable HTTP, at `/mcp`)
   that offers the profile's tools to outside assistants. It has no tools of its own: every call
   goes through the gateway as the profile's outside agent, so OPA, role filtering and audit work
   exactly as for the planner. Callers never get the addresses of the tool servers.

2. **Configured in the profile.** `outside.yaml` names the agent grant outside callers use, where
   the endpoint listens, how callers are identified, and when results are masked:

       agent: outside
       listen: {host: 127.0.0.1, port: 8110}
       auth:
         kind: introspection
         url: http://127.0.0.1:8300/introspect
         secret_env: PLATFORM_INTROSPECT_SECRET
         cache_seconds: 30
       mask_results: external          # external · always · never

   The loader refuses a profile when the outside agent is not granted, is the planner, or holds a
   tool that changes something (outside assistants never change anything directly), and when the
   secret is written into the file instead of named as an environment variable.

3. **Who is calling comes from the organization, not the caller.** A caller sends a bearer token.
   The endpoint asks the organization's own system who it is (token introspection, RFC 7662 style,
   with a shared secret): client ID, tenant, role, and whether the client runs locally or at an
   outside AI service. Any failure means no access (HTTP 401, audited). The answer is cached for
   at most `cache_seconds`, so a revoked client stops within that time. No outside identity
   provider is needed: the organization's own user database answers.

4. **Which tools.** A caller sees and may call only the tools its role may use (`roles.yaml`), among
   those granted to the outside agent.

5. **Tenants are fenced.** A tool marked `tenant_scoped: true` in `tools.yaml` holds several
   tenants' data. The gateway sets `tenant_id` to the caller's tenant (overwriting anything sent),
   OPA denies the call without a tenant (`policies/mcp_authz.rego`), and the gateway drops any
   returned record of another tenant, or with none, and audits how many it dropped. Tenant-scoped
   tools may only be granted to the outside agent, the one component that knows a caller's tenant.

6. **The checks run on tool arguments.** Text in a call's arguments goes through the same input
   checks as a question (`guardrails/check.py`: rules, then the classifier with the profile's
   narrow exemptions). A crisis is not passed on as a query: the caller gets the profile's crisis
   reply and `hand_over: true`. A decision that must stay with a person is declined with the
   profile's reply. If the classifier cannot run, the call is blocked.

7. **What leaves.** Results for clients at an outside AI service are masked (Presidio), unless the
   profile says otherwise. Data the profile marks as never leaving (`routing.never_external_labels`)
   is refused to those clients altogether.

8. **Audit.** Guard decisions (`audit.guardrails`, `stage: tool_input`), tool calls and refusals
   (`audit.tool_calls`) carry the client, tenant and role, in the same tamper-evident chain. Only
   masked arguments are recorded. The token itself is never stored or logged.

9. **Profiles outside the repository.** `PROFILE_DIR=/path/to/folder` loads a profile kept outside
   this repository, so an organization keeps its configuration in its own folder and connects
   without changing this code.

10. **Questions typed into an assistant with its own model.** An assistant that runs its own
    model (for example a chat panel inside the organization's app, using a local model) sends
    each question to `POST /check` with its bearer token before its model sees it. The question
    gets the same checks as tool text and the same fixed reply if stopped
    (`audit.guardrails`, `stage: user_message`; the text itself is not recorded). If masking or
    the checks fail, nothing is run.

11. **Governed chat: one question, one pipeline request.** An assistant the organization builds
    can hand a whole question to the pipeline under a request ID it makes up (`chat-<uuid>`):
    - `POST /check` with that ID checks the question;
    - tool calls over MCP with the header `X-Request-Id` follow, accepted only after an allowed check and only from the same client;
    - `POST /answer` then makes the pipeline write the answer. It masks the question and the tool data together, routes the request (`router.route`), asks the model, runs the output checks (rules, then the classifier) and publishes `chat.responses`.

    Each step is audited and traced (`agent.trace`, process `endpoint`) under the one ID, so the dashboard shows the request moving through all eight stages. Chat state lives in memory for 10 minutes. A tool call stopped by the checks ends the request.

12. **The organization's administrator decides about outside models.** The introspection answer
    may carry `external_model_allowed`. It is off unless the organization's system says true, and the token check passes it along. When it is off, routing stays local with the reason "the organization's administrator has not allowed outside AI models", before the routing policy is asked. When it is on, the profile, the routing policy (no personal information, never `never_external_labels` data) and an API key must all agree too. Only masked text leaves.

13. **Not in this step.** Proposing changes from an outside assistant (through the approval flow
    of ADR 009), rate limits per client, and OAuth sign-in for MCP clients.

## Consequences

- An organization can let any MCP assistant use its tools under the same rules as the project's own
  assistant: who may see what, decisions that stay with people, crisis hand-over, audit. Switching
  assistants is configuration, not code.
- A platform serving many customers can expose one endpoint; each customer's assistant sees only
  that customer's data, enforced in three places (the endpoint, OPA, the gateway's filter).
- **Limit:** the endpoint governs what an outside assistant does with tools and data. It does not
  see the rest of that assistant's conversation, unless the assistant sends each question to
  `/check`, which only assistants the organization builds itself can be relied on to do.
- The organization's introspection service becomes part of the trust boundary: it must be reachable
  only by the endpoint (shared secret, local network) and answer from its own user records.

- The live dashboard (`ui/dashboard.html`) shows endpoint requests (IDs starting `out-`) with their
  own stages: caller identified, text checked, role allows the tool, data may leave, tool gateway
  and policy, tenant fence. A caller that asks for another tenant is recorded
  (`decision: override`, `tenant_requested`) and served its own tenant. With `PROFILE_DIR` set the
  dashboard shows only that profile's events and verifies only the audit chains that wrote for it
  (`/audit/verify?scope=all` checks every chain); `DASHBOARD_CHAT=off` hides the pipeline's own chat
  for a profile served only through the endpoint.
- **Changed by ADR 016:** the organization's `external_model_allowed` now also decides whether
  outside assistants (`location: external`) may use the tools at all; off, every call is refused and
  audited. Tool results are checked for prompt injection in the gateway before any assistant reads them.

## Tests

`tests/test_endpoint.py`: outside.yaml checks, tenant-scoped grants, `PROFILE_DIR`, the gateway's
tenant enforcement, role-based tool lists, crisis and decision arguments, never-external data,
masking modes, masked audit arguments, plain errors, governed chat requests (every stage, same
client only, expiry, stopped tool calls, the administrator's setting reaching routing), and introspection (identity, fail-closed,
caching, unknown locations), and `/check` (token, text limits, crisis reply, audit without the text). `policies/tools_test.rego`: tenant-scoped tools need a tenant.
