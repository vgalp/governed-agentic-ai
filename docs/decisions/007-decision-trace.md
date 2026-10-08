# 007: A live decision trace on Kafka

Status: Accepted · Date: 2026-10-07

## Context
The audit topics record every decision, but they are written for verification, not for
reading: a reviewer has to piece a request together from four topics. Staff, compliance
reviewers and evaluators need to see, step by step and as it happens, why a request was
answered, limited or blocked, and which rule or policy decided it.

A model's own "reasoning" text is not a suitable explanation. It is not checked by the
guardrails before it would be shown, it may contain personal details when the local model
sees the original text, and it is not a reliable account of why the model answered as it did.

## Decision
- Every step of a request is published to a new Kafka topic, `agent.trace`, as it happens:
  received, input rules, input classifier (including overrides), role check, masking, each
  tool call, routing, the model call, output rules, grounding, output classifier, reply.
- Each event names the **agent** that owns the step (intake, knowledge, records, router,
  answer), the step, its outcome (running, pass, block, override, info, error), a one-line
  summary of the decision and the rule or policy behind it, and timing.
- Slow steps (classifiers, tool calls, the model) publish a `running` event first, so the
  dashboard shows work in progress.
- **No personal data.** Summaries hold categories, counts, kinds of masked details and
  source IDs, never messages, answers or names (tested).
- The trace records the pipeline's decisions, not the model's internal reasoning.
- The audit topics remain the record of truth (tamper-evident). The trace is the readable
  view and is not hash-chained.
- The dashboard shows the trace per request: one lane per agent and the steps in order.

## Consequences
- Today one process (the planner) runs every step. The agent names already match the
  planned multi-agent pipeline, where each agent runs separately and talks over Kafka; the
  trace and the dashboard then work unchanged.
- Because the trace is a Kafka topic, a past request can be replayed step by step (next
  step), and other consumers, such as a compliance audit agent, can subscribe without any
  change to the pipeline.
- A few extra small messages per request; they are sent without waiting, so answers are not slowed.