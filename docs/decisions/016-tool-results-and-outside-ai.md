# 016. Prompt injection in tool results; one switch for outside AI

Status: Accepted · Date: 2026-10-17

## Context

The input guardrails check what a person types, and the endpoint checks the text an assistant puts
in a tool's arguments (ADR 015). Text that comes **back** from a tool was not checked: a record note,
a policy document or an email body went to the model as it was. A document that says "ignore your
previous instructions" or hides instructions in invisible characters could steer the model that
reads it (indirect prompt injection). Tool results also reach outside assistants (Kiro, Claude) through
the governed endpoint.

Separately, an organization's administrator could allow or refuse an outside **model** for the
pipeline's own answers (ADR 015, decision 12), but outside **assistants** with a token could use the
tools either way. One organization had two separate decisions about outside AI, and the second was
easy to miss.

## Decision

1. **Every tool result is checked in the gateway** (`guardrails/injection.py`), after the policy and
   tenant filters and before the result reaches any model or outside assistant. One place covers the
   pipeline's chat, governed chat requests and outside assistants.
2. **What is checked:** fixed patterns for the known forms: instructions to ignore or override the
   rules, "new instructions:", "you are now", fake role markers (`system:`, chat-template tokens),
   requests to reveal the prompt, instructions to hide something from the user, instructions to send
   data to an address, and markdown images whose URL carries data out. Invisible characters that can
   hide text (Unicode tag characters, runs of zero-width characters) are always removed.
3. **What happens:** the matching text field is withheld, replaced by a notice that names the tool and
   says why; the rest of the row and the other rows are kept. The call's audit event records how many
   fields were withheld and which kinds of pattern matched (`injection_withheld`, `injection_kinds`);
   the dashboard shows the gateway stage as flagged.
4. **The answering model is told** that the data it is given is information, never instructions.
5. **One switch for outside AI.** The organization's `external_model_allowed` (sent with the token
   check) now also decides whether outside assistants may use the tools at all. Off: every tool call
   from a client marked `external` is refused and audited ("outside AI not allowed by the
   organization"). Local assistants are not affected.
6. **Role limits in answers.** When the pipeline writes an answer (governed chat), the model is told
   which tools the person's role cannot use, so it says "your role does not have access" rather than
   "there is no data".

## Consequences

- An injected instruction in a record or document no longer reaches the model or an outside assistant
  in the forms above, and each case is visible in the audit and on the dashboard.
- **Limit:** fixed patterns catch the common, known forms; a determined attacker can word an
  instruction that matches none of them. This check is one layer, beside read-only tools, the
  "data is not instructions" prompt, the output checks and the audit. A classifier for injection is a
  possible later layer.
- The patterns were run over every piece of tool data in this repository and the Meridale demo
  (knowledge bases, policies, profile data: 415 strings) with no false matches, and over ordinary
  sentences that share words with the patterns ("ignore the noise", "the previous instructions from
  the physician", "send the invoice to the payer").
- Organizations that already gave tokens to outside assistants must switch outside AI on for them to
  keep working.

## Tests

`tests/test_injection.py` (known forms found, ordinary text passes, hidden characters removed, only the
suspicious field withheld, gateway withholds and audits). `tests/test_endpoint.py` (outside assistants
refused unless allowed, local unaffected; role limits given to the answering model).
