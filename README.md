# Governed Agentic AI Framework

An open-source reference architecture for deploying multi-agent AI systems safely in healthcare, government, and other regulated environments.

> **Status:** Early development. This is a research prototype, not production software.

## Why

Organizations want to use AI agents, but running them on sensitive data is risky. Agents can expose personal information, take actions they shouldn't, fail halfway through a task, or give unsafe answers. Each organization currently solves these problems on its own. This project provides an open, tested architecture that puts safety controls around any AI agent system.

## Architecture (planned)

1. **Event-driven backbone (Apache Kafka):** agents communicate through a broker, so failures don't cascade and work can be retried or rolled back.
2. **MCP gateway with policy:** each agent has its own identity and may only access the tools and data its policy allows; every request is audited.
3. **Privacy layer:** personal and health information is masked before any model sees it.
4. **Guardrails:** every response is checked against approved content and safety rules before it is shown.
5. **Tracing and audit:** every step is recorded, so any answer can be explained afterward.

## Demonstrations (planned)

- **Healthcare:** a self-management assistant for adults with ADHD, built and evaluated with clinical collaborators.
- **Government:** a public-service case-processing workflow.

## Quick start

Requirements: Docker and Docker Compose.

```bash
docker compose up -d
```

- Kafka: `localhost:9092`
- Schema Registry: `http://localhost:8081`
- Kafka UI: `http://localhost:8080`

## Roadmap

- [x] Event backbone (Kafka, KRaft mode) and Schema Registry
- [ ] Chat API and first agent with a local model
- [ ] Privacy layer (PII/PHI masking)
- [ ] MCP gateway with per-agent policy and audit
- [ ] Guardrails and evaluation suite
- [ ] Tracing, failure recovery, and evaluation report

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