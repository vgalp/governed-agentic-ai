# 020: An OpenAI-compatible endpoint, so existing chatbots can adopt the layer by configuration

Status: Accepted · Date: 2026-10-30

## Context
The governance layer is meant to sit in front of the AI assistant an office already uses. Most
chatbots, chat apps and libraries talk to a model through the OpenAI chat format
(`POST /v1/chat/completions`). Until now the pipeline accepted only its own `/chat` request (submit,
then poll for the answer), so adopting it meant changing the chatbot's code.

## Decision
The API also serves the OpenAI chat format (`api/openai_compat.py`):
- **`POST /v1/chat/completions`.**
  - The last user message is the question and the earlier messages are the conversation (ADR 019).
  - The request goes through the same `submit` function as `/chat`, so it passes every check:
    rules, guard classifier, conversation check, masking, roles, routing, answer checks and audit.
  - The endpoint waits for the answer and returns it in the OpenAI response shape.
- **`GET /v1/models`** lists one model, `governed-<profile>`. Many clients ask for it first.

What the caller can and cannot control, and why:

| The client sends | What happens | Why |
|---|---|---|
| A `system` or `developer` message | Dropped, never sent to the model; counted in `governance.dropped_system_messages` | The office's prompt sets its rules (no doses, no eligibility decisions, cite sources). A client prompt could switch them off. |
| `tools`, or `tool` messages | Refused (400) | The profile decides which tools exist and who may use them (ADR 010). |
| Image or audio parts | Refused (400) | The checks read text only. |
| `model` | Accepted and ignored | The profile's routing decides, and keeps personal details local (ADR 004). |
| `user` | Used as the caller's id in the audit | As `user_id` in `/chat`. |
| `stream: true` | The answer is sent as server-sent events, all at once | Every answer is checked in full before any of it is released. Word-by-word streaming would show text that a check might still stop. |

What the client gets back:
- **The answer, or the office's fixed reply** when a check stopped it.
- **`finish_reason` is always `stop`, never `content_filter`.** Some chat apps hide a reply marked as
  filtered, and a crisis hand-over must be shown.
- **A `governance` field.** OpenAI clients ignore it. It holds the request id (to find the request
  in the audit trail and the dashboard), whether a check stopped the answer and which one (stage,
  layer, category), which model answered, and which models checked it. It never includes message
  text or the guard's reasons.

**Access.** Callers send `Authorization: Bearer <key>`, checked against `OPENAI_COMPAT_API_KEYS`
(comma-separated, compared in constant time).
- Without keys the endpoint accepts anyone, which is for local development only. The API logs a
  warning at startup.
- The `X-Governed-Role` header sets the staff role for record access. Like `role` in `/chat`, this
  is for demos only: a real deployment takes the role from its own sign-in (as the governed MCP
  endpoint does, ADR 015).

## Consequences
- **An existing chatbot adopts the layer by changing its base URL and key.** No code changes.
- **No word-by-word streaming:** the price of checking whole answers. Chat apps that stream still
  work; the text appears at once after the checks.
- **The request waits for the pipeline,** up to `OPENAI_COMPAT_TIMEOUT` seconds (default 180), then
  returns 504 with the request id.
- **The answer is held in the API's memory,** as for `/chat`. Replacing that store, and per-key
  users and roles, are needed before running several API instances.
- **This changes how a chatbot connects, not which questions it handles well.** A profile written for
  record look-ups still answers only from records. An everyday chatbot needs its own profile, with
  its own prompt and guard policy, and crisis replies written for members of the public rather than
  staff.
