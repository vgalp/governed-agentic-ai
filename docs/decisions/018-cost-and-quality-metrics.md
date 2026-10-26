# 018: Cost and quality metrics: cost per task, iteration cost, task success, toxicity

Status: Accepted · Date: 2026-10-25

## Context
On 2026-10-25 an independent reviewer, a quality engineer with 15 years' experience who had not
worked on the project, ran the system through its use cases. All of them worked and the guardrails
held. The review raised two points:
- **A defect.** The dashboard's "Verify audit chain" button gave no visible result: it re-checked
  the chain, but the badge showed the same text before and after.
- **Missing operational metrics.** An office deciding whether to run an AI assistant asks basic
  questions the dashboard could not answer:
  - What does a request cost?
  - How many model calls does one take?
  - How often does a request end as intended?
  - How toxic are the answers?

The reviewer suggested cost per task, iteration cost, tool-use accuracy, toxicity (Detoxify) and
task success rate.

## Decision
Every request is metered while it is handled, and its summary goes out with its response. The
dashboard adds the summaries up. No text is ever kept: only counts, times, model names, scores and
the outcome.

**Metering** (`llm/usage.py`).
- The planner starts a meter for each request. Every model call records itself on that meter:
  - the answering model;
  - the model that prepares a proposed change;
  - the input agent;
  - Llama Guard, on questions and answers;
  - the guard classifier.
- Each call records its purpose, model, provider, local or external, tokens in and out, time,
  whether it worked, and its cost.
- Tokens are the provider's own counts: `usage` from OpenAI-compatible APIs and Ollama, `usage`
  from Claude, and `prompt_eval_count` / `eval_count` from Ollama's generate API for Llama Guard.
  If a model returns no count, the tokens are estimated at about four characters per token and
  marked as estimated. The guard classifier is a classifier, so it has no tokens; it counts as a
  call with its time.

**Cost per task.**
- Cost is tokens times the model's price in the profile:
  `price: {input_per_million, output_per_million}` in US dollars, validated by the loader.
- Local models count as $0. The cost of running your own hardware is not included, and the
  dashboard says so.
- An external model with no price is reported as "no price set", never as $0, so a missing price
  can never look free.
- Prices are left for each office to fill in from its provider's pricing page. Prices change, and
  a stale number in a public repository would mislead.

**Iteration cost.** Model calls per request, including the safety checks, with the breakdown by
purpose. Also tokens per request and time spent in models.

**Task success rate.** Each request ends with exactly one outcome, taken from what was published
(`usage.outcome_of`):

| Outcome | Meaning | Success? |
|---|---|---|
| answered | the assistant answered (a model, or a tool's source text) | yes |
| proposed | a change was proposed for a person to approve | yes |
| declined | a policy decided before the model: crisis hand-off, forbidden decision, role, tool | yes |
| withheld | the model wrote an answer that failed the output checks; a fixed reply was sent | no |
| failed | a safety check or model was unavailable, or a requested change was unclear | no |
| no reply | no response 2 minutes after the request (counted by the dashboard) | no |

A crisis hand-off counts as a success: for a staff assistant, the intended end of a crisis message
is a person, not an answer. The dashboard always shows the breakdown next to the rate, so nobody
has to accept that definition on trust.

This is *operational* success: the request reached a sound end. Whether an answer was *correct*
needs labelled questions and is measured offline (the frozen test sets, the crisis benchmark). The
reviewer's tool-use accuracy is likewise an offline measure, left for a labelled set of questions
with the tool each should use.

**Toxicity** (`guardrails/toxicity.py`).
- Detoxify's "original" model (`unitary/toxic-bert`, published by the Detoxify authors, Apache 2.0)
  scores every answer the answering model writes, locally, before the output checks.
- A long answer is scored in parts, and the highest part counts.
- It is a measurement, not a guardrail. It never blocks: the output rules and Llama Guard decide,
  with the profile's policy. A score of 0.5 or more is marked "high: shown for review" in the trace
  and counted on the dashboard.
- It is off with `TOXICITY=off`, and off if the model cannot load (it needs the train group). The
  dashboard then says "not measured", never 0.
- The `transformers` model is used directly instead of the `detoxify` package: same weights, no
  extra dependency.

**The defect.**
- The dashboard's verify button now shows "Checking…", then the result with the time, and a note
  saying what was checked and what an intact chain proves.
- While adding the "failed" outcome, a second defect was found and fixed: if the local model (the
  last fallback) failed, the planner raised the error and the process stopped. It now records the
  failure, sends the profile's "unavailable" reply, and carries on with the next request.

## Consequences
- **Where the numbers appear.** The selected request shows its model calls, tokens, cost, time,
  toxicity and outcome. A "Cost and quality" panel shows, over all measured requests:
  - task success, with the breakdown by outcome;
  - cost per task and total cost;
  - model calls, tokens and time in models per task;
  - the highest toxicity, and the number of answers with high toxicity.
- **Earlier requests.** Requests from before this change carry no metrics and are not counted.
- **Where it is recorded.** The summary travels on `chat.responses`, not in the decision trace: it
  is a measurement, not a decision, and it would otherwise mark the "answer" lane as used for
  requests that never reached a model.
- **Cost of measuring.** Toxicity adds one local classifier pass per model answer, tens of
  milliseconds on a laptop GPU. Its model, about 440 MB, downloads from Hugging Face the first
  time the planner starts.
- **Prices.** Prices are per profile, so two offices with different contracts see their own costs.
- **Credit.** The reviewer's suggestions shaped this ADR. Tool-use accuracy is recorded here as
  the next measure, once a labelled set of questions exists.
