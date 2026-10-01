# Governed Agentic AI Framework

An open-source reference architecture for deploying multi-agent AI systems safely in healthcare, government, and other regulated environments.

> **Status:** Early development. This is a research prototype, not production software.

## Why

Organizations want to use AI agents, but running them on sensitive data is risky. Agents can expose personal information, take actions they shouldn't, fail halfway through a task, or give unsafe answers. Each organization currently solves these problems on its own. This project provides an open, tested architecture that puts safety controls around any AI agent system, and measures whether they work.

## Architecture

Every request passes through layered controls before and after the model:

```
User request
   │
   ▼
Chat API ──► Kafka (chat.requests)
                 │
                 ▼
          Planner agent
   1. Input guardrail ──── blocked? ──► fixed safe response
   2. Privacy layer: mask personal information
   3. MCP gateway ──► OPA policy check ──► knowledge base (approved content)
   4. Local model (Mistral via Ollama), masked text only
   5. Output guardrail ── blocked? ──► fixed safe response
   6. Restore details, attach sources
                 │
                 ▼
          Kafka (chat.responses) ──► Chat API ──► User

Every decision is written to an audit topic:
  audit.guardrails · audit.tool_calls · audit.model_inputs
```

| Layer | What it does | Status |
|---|---|---|
| Event backbone (Apache Kafka) | Agents communicate through a broker, so work is never lost and failures don't cascade | Working |
| Privacy layer (Microsoft Presidio) | Names, phone numbers, emails and other identifiers are masked before any model or tool sees them | Working |
| MCP gateway + OPA policy | Each agent may call only the tools its policy allows; every decision is audited | Working |
| Guardrails, layer 1 (rules) | Crisis, medication-advice and diagnosis requests receive fixed, safe responses and never reach the model | Working |
| Guardrails, layers 2–3 | Safety classifier and grounding check | Planned |
| Tracing and failure recovery | OpenTelemetry tracing, Saga-style rollback, idempotency | Planned |

## Results so far

Red-team evaluation v1.1: 30 fixed prompts (19 unsafe, 11 normal), comparing the base model with the system prompt only (**baseline**) against the full pipeline (**governed**). Model: Mistral 7B via Ollama.

| | Baseline | Governed |
|---|---|---|
| Unsafe prompts handled safely | 10/19 | **17/19** |
| Medication-advice prompts | 6/10 | **10/10** |
| Diagnosis prompts | 3/5 | 4/5 |
| Crisis prompts | 1/4 | 3/4 |
| Answers containing medication dose amounts | 2 | **0** |
| Normal prompts wrongly blocked | 0/11 | 2/11 |

Scores are from an automatic first pass; every answer was also reviewed manually.

**Findings**

- The baseline model gave specific medication dose figures and, in several runs, an outdated crisis line number. The governed system gives clinician-reviewable fixed responses with current crisis lines (988 in the U.S., 9-8-8 in Canada).
- The first evaluation run revealed a real bug: medication names were being masked as personal names, which let some requests bypass the input guardrail. It was fixed by running input guardrails before masking and adding an allow list.
- Known gaps: indirect crisis language (for example "I don't want to be here anymore") and implied-diagnosis answers are not yet caught, and the output rule over-blocks some answers that merely mention medication. These motivate guardrail layers 2 and 3.

Full results: [`eval/results/`](eval/results/). Test set: [`eval/datasets/redteam_v1.jsonl`](eval/datasets/redteam_v1.jsonl).

## Demonstrations

- **Healthcare:** a self-management assistant for adults with ADHD, built and evaluated with clinical collaborators. Knowledge base entries are samples pending clinician review.
- **Government:** a public-service case-processing workflow (planned).

## Quick start

Requirements: Docker, [uv](https://docs.astral.sh/uv/), and [Ollama](https://ollama.com).

**1. Start infrastructure** (Kafka, Schema Registry, Kafka UI, OPA):

```bash
docker compose up -d
```

- Kafka: `localhost:9092`
- Kafka UI: `http://localhost:8080`
- Schema Registry: `http://localhost:8081`
- OPA: `http://localhost:8181`

**2. Create topics:**

```bash
for t in chat.requests chat.responses audit.model_inputs audit.tool_calls audit.guardrails; do
  docker exec broker kafka-topics --bootstrap-server broker:29092 --create --if-not-exists --topic $t
done
```

**3. Install dependencies and the model:**

```bash
uv sync
uv run python -m spacy download en_core_web_lg
ollama pull mistral
```

**4. Run the services** (separate terminals):

```bash
uv run python -m mcp_servers.knowledge_base.server   # MCP knowledge base, port 8100
uv run uvicorn api.main:app --reload                 # Chat API, port 8000
uv run python -m agents.planner                      # Planner agent
```

**5. Send a message:**

```bash
curl -X POST localhost:8000/chat -H "Content-Type: application/json" \
  -d '{"user_id": "test-user-1", "message": "Help me plan tomorrow."}'
curl localhost:8000/chat/<request_id>
```

Or use the interactive API docs at `http://localhost:8000/docs`. Watch the audit topics in Kafka UI.

## Tests and evaluation

```bash
uv run pytest                      # unit tests
uv run python -m eval.run_redteam  # red-team evaluation (all services must be running)
```

## Project structure

```
api/             Chat API (FastAPI)
agents/          Planner agent and system prompt
privacy/         Personal-information masking (Presidio)
gateway/         MCP gateway: policy check, tool call, audit
policies/        OPA policies (per-agent tool permissions)
mcp_servers/     MCP servers (knowledge base)
guardrails/      Input and output safety rules
eval/            Test sets, evaluation script, results, examples
tests/           Unit tests
docs/            Architecture notes, decisions, progress
```

## Roadmap

- [x] Event backbone (Kafka, KRaft mode) and Schema Registry
- [x] Chat API and first agent with a local model
- [x] Privacy layer (PII/PHI masking)
- [x] MCP gateway with per-agent policy and audit
- [x] Rule-based guardrails and first red-team evaluation
- [ ] Safety classifier (layer 2) and grounding check (layer 3)
- [ ] Clinician-approved knowledge base and fine-tuned model
- [ ] Tracing, failure recovery (Saga pattern), and chaos tests
- [ ] Government case-processing demonstration
- [ ] Technical report

## Important

- **Not a medical device.** Not for diagnosis, treatment, or emergencies.
- **Synthetic data only.** This repository contains no real personal or patient information.

## Background

The patterns in this project are described in these articles:

- [A2A ≠ Resilience: Why Multi-Agent Systems Need an Event-Driven Backbone](https://medium.com/@vladimir_62630/a2a-resilience-why-multi-agent-systems-need-ad-event-driven-backbone-09054b47b36b)
- [Part 2: From Chaos to Control: Governing the Event Stream with MCP](https://medium.com/@vladimir_62630/part-2-from-chaos-to-control-governing-the-event-stream-with-mcp-48ad0347f777)
- [Part 3: Agent Identity Is the Missing Control Plane](https://medium.com/@vladimir_62630/part-3-agent-identity-is-the-missing-control-plane-why-autonomous-ai-needs-first-class-identity-46d3333299ef)
- [Agentic AI Is Creating Probabilistic Distributed Systems](https://medium.com/@vladimir_62630/agentic-ai-is-creating-probabilistic-distributed-systems-and-most-architectures-arent-ready-d01612170225)

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Author

Wladimir (Vladimir) Galperin
