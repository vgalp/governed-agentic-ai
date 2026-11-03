# Architecture

A governance layer that sits between people and AI models in a records-holding office (a clinic, a
county benefits office, a school) or in front of a public assistant. It decides, for every message,
what no model may do, what a model may see, which tools may run, which model answers, and what the
person gets back — and it records every decision. The models are replaceable; the layer is the
product. For the reasoning behind each part, see the ADRs in `docs/decisions/` (numbers below).

## One request

```
 person ──► API (api/main.py) ──► Kafka chat.requests
                                       │
                          ┌────────────▼─────────────┐
                          │  planner (agents/pipeline) │   one process, one request at a time
                          │                            │
            1 intake      │  rules ─► classifier ─► input agent ─► conversation        (ADR 006, 019)
            2 role_check  │  unknown role: no records, no answer                       (ADR 005)
            3 masking     │  Presidio: [PERSON_1] … before any tool or model           (privacy/)
            4 detect_change  a change is a proposal, never an answer                  (ADR 009, 012)
            5 call_tools  │  ──► gateway ──► OPA (may this role use this tool?) ──► MCP tool
                          │       ▲ audit ▲ injection check on results               (ADR 010, 016)
            6 settle_tools│  direct-answer tools: the tool's text IS the answer       (ADR 017, 023, 024)
            7 propose     │  a change request → approval for a second person          (ADR 009)
            8 answer      │  route (OPA: local or external) → ask → fall back          (ADR 004, 008, 018)
            9 check_answer│  rules ─► grounding ─► classifier ─► faithfulness judge    (ADR 006, 022)
                          └────────────┬─────────────┘
                                       ▼
                 Kafka chat.responses · agent.trace (live steps) · audit.* (tamper-evident chain)
                                       │
 person ◄── API ◄──────────────────────┘        executor: makes an approved change, nothing else (ADR 009)
```

Each stage is a function in `agents/pipeline/stages.py` that returns an `Outcome` when the request
ends there (a fixed reply, a direct answer, a proposal) or `None` to pass it on. The output checks
are a list in `agents/pipeline/checks.py`; adding one is adding a function. `agents/planner.py` is
the process around the pipeline: Kafka in, one request, Kafka out. Everything the pipeline needs
from outside (the producer, the audit chain, the model client, the gateway, the masker, the policy
engine) comes in one `Runtime` object, so `tests/test_pipeline.py` runs the whole pipeline with fakes.

## Where things live

| Concern | Where | Notes |
|---|---|---|
| What an office allows | `profiles/<name>/*.yaml` | models, rules, replies, tools, roles, actions, records map, privacy; validated by `profiles/loader.py` at startup (ADR 003, 011) |
| Policy decisions | `policies/*.rego` (OPA) | tool access per role, routing, who approves a change; tested with `opa test` |
| Input and output rules | `profiles/<name>/rules.yaml` | regex layer: crisis words, decisions, triggers for tools; a flag can only be made stricter, never looser for crisis |
| Safety classifiers | `guardrails/classifier.py`, `classifier/` | the fine-tuned guard (ModernBERT, published on Hugging Face) for questions; Llama Guard for answers; frozen test sets and benchmarks in `eval/` (`eval/results/guard_v6_notes.md`) |
| Personal data | `privacy/` | Presidio masking; the mapping never leaves the process; audit and trace hold masked text only |
| Tools | `mcp_servers/*` | MCP servers, one per source: records (SQLite or Epic FHIR), knowledge base, medication review, chart brief, drug reference, web research, changes |
| The only path to a tool | `gateway/gateway.py` | OPA check, audit, result filtering by role and tenant, injection check, progress messages |
| Audit | `audit/` | hash-chained, optionally HMAC-keyed events on Kafka; `audit/verify.py` checks the chain (ADR 002) |
| Outside assistants | `gateway/endpoint.py`, `api/openai_compat.py` | an MCP endpoint and an OpenAI-compatible chat endpoint, so other assistants run behind the same layer (ADR 015, 020) |
| UI | `ui/` | demo page, plain chat, engineer dashboard; viewers of the trace (ADR 007) and the responses |
| Evaluation | `eval/`, `tests/` | ~800 unit and pipeline tests; frozen crisis test sets; chatbot comparison; chart brief accuracy run |

## The rules the design keeps

1. **No model decides what an office must not delegate.** Crisis, dosing, diagnosis, eligibility,
   discipline: rules and the classifier stop these before any model runs; a direct-answer tool's
   text is the answer where facts matter.
2. **No model sees what it does not need.** Masked text to every model; the original only to local
   tools that must look a person up (and the audit still records the masked text); place names
   restored only where a profile says so.
3. **No tool runs without policy.** One gateway, OPA on every call, results filtered to the role's
   data classes, page text checked for instructions before a model reads it.
4. **No change without a person.** The planner proposes; a second person with the right role
   approves; the executor is the only process that can change anything.
5. **Everything is recorded.** Every decision, with its layer and category, in a tamper-evident
   chain; a live trace with no personal data for the people watching.
6. **An office changes configuration, not code.** A new office is a profile folder; the loader
   refuses unsafe combinations.

## Threat model (short form)

| Threat | Where it would enter | What stops it | Residual risk |
|---|---|---|---|
| A model gives medical, legal or financial decisions | the answer | input rules and classifier before the model; output rules and classifier after; direct tools for facts | classifier misses (measured on frozen sets: crisis-first thresholds; v6.1 reported with its false-alarm rate) |
| A crisis message is answered as routine | the question | crisis rules, the guard classifier, the conversation check, the input agent; crisis flags can never be exempted (loader refuses) | wording outside the training distribution (role-play, adversarial: v7 work) |
| Personal data reaches an outside model or a search engine | tools, routing | masking before tools and models; OPA routing on entity types and data labels; the research tool refuses placeholders; the audit holds masked text | Presidio false negatives on unusual formats; the allow-list for drug names is a deliberate, reviewed exception |
| A page or record instructs the model (prompt injection) | tool results | the gateway withholds text that looks like instructions before any model sees it; notes are labelled "information, never instructions" | novel phrasings; the judge is the second line for web answers |
| A model invents a source or a fact | the answer | grounding check (any identifier-like citation not given is blocked); faithfulness judge for web answers; direct tools for records | a 7–8B judge is wrong both ways; measured by the comparison set |
| A role sees data it may not | tools | OPA per tool and role; the records servers read only allowed data classes; the gateway drops anything else and counts it | a tool server bug over-returning (counted as `dropped_by_gateway`, which should always be 0) |
| An unauthorised change to a record | the executor | the planner cannot change anything; OPA decides approvers; approvals expire; the executor acts only on an approved proposal | the demo takes the role from the UI; a deployment must take it from the sign-in (OIDC/SMART) |
| Tampering with the audit | Kafka | hash chain, optional HMAC key, `audit/verify.py` | key management is the operator's |
| A tool failure takes the service down | gateway | a failed or non-JSON tool reply is "no results"; one failing FHIR resource type does not lose the others; a request that raises gets a fixed reply and an audit entry | — |

## Known limits (honest list)

- The role comes from the UI in the demo; production needs it from the sign-in.
- Models run serially on one machine; the seconds are the models, the layer adds milliseconds.
- The regex rule layer is explicit and testable but brittle to phrasing; an intent classifier is the
  planned replacement for triggers (the guard training pipeline exists).
- Service addresses are per-profile configuration for a laptop; a deployment wants a registry,
  containers per service and health endpoints.
- Kafka is a hard dependency; an in-memory bus for development and tests is planned.
- The UI knows the pipeline's step names; it should read labels from the trace.
