# 030: Confirmed delivery, supervision, audit checkpoints and locked-down services

Status: Accepted · Date: 2026-11-08

## Context
The November audit's remaining blockers and majors were about what happens when something goes
wrong, and about who else on the machine can reach the parts: a Kafka message could be lost without
anyone noticing (OC-2); a crashed service stayed down and `/health` always said "ok" (OC-3); one
person could send unlimited messages (SC-10); deleting the newest audit events left a valid chain
(ADR 002's known limit); OPA's API, which can replace policies, answered any local process (SC-5);
and tool servers trusted the tenant and data classes from any local caller (SC-6).

## Decision
- **Confirmed delivery** (`ops/delivery.py`). Every producer is wrapped: each message has a
  delivery callback; failures are counted and logged with the topic and error, never the content;
  a lost audit event is reported as a gap. Where a person waits (a chat message, an approval
  decision) the API waits for the broker's confirmation and answers 503 "try again" instead of
  handing out a request ID nothing will answer.
- **Liveness and supervision** (`ops/health.py`, `ops/supervise.py`). The planner and executor
  write a heartbeat file; a worker in a long request says "working", so a slow laptop is not a dead
  worker (stuck only after 15 minutes). `/health` checks the broker, the API's three Kafka reader
  threads, the workers and delivery: `ok`, `degraded` (HTTP 200, with warnings) or `down` (HTTP 503).
  `scripts/dev.sh` runs every service under a supervisor that restarts it after 1, 2, 4 ... seconds
  and gives up, visibly in `/health`, after 5 crashes in 2 minutes. `./scripts/dev.sh health`.
- **Rate limit per person** (`ops/ratelimit.py`). A token bucket per signed-in person (per network
  address in demo mode) on `/chat` and `/v1/chat/completions`: by default 10 at once, then 20 a
  minute; `rate_limit` in profile.yaml changes it. Over the limit: 429 with Retry-After.
- **Signed audit checkpoints** (`audit/chain.py`). Each chain appends `{seq, hash}` of its newest
  event to its own file outside Kafka: every 50 events, at most a minute after its newest event,
  and at exit. Lines are signed (the audit key) and linked. `/audit/verify` and `audit.verify` now
  find newest events deleted, the checkpointed event rewritten, or a whole chain gone, and a
  changed checkpoint file. Checkpoints younger than 30 s are not judged, while events still arrive.
- **OPA requires a token** (`policies/system/authz.rego`). OPA runs with token authentication;
  only `OPA_TOKEN` is accepted (made once by `scripts/dev.sh` in `.env`; the gateway reads it from
  the environment or `.env`). Without `OPA_TOKEN` OPA stays open, so a first start still works.
- **Mutual TLS to the tool servers** (`scripts/certs.sh`, `mcp_servers/common/serve.py`,
  `gateway/transport.py`). With `GOVERNED_MTLS=on`, a local CA issues one server certificate and a
  gateway client certificate; the repository's tool servers serve HTTPS and accept only the
  gateway's certificate; the gateway switches their `http://127.0.0.1:81xx` addresses to `https`
  itself, so profiles are unchanged. Other platforms' tool servers are called as configured. Every
  tool server now starts the same way (`serve(mcp)`), the first step of MT-1.

## Consequences
- Tested here: unit tests for each part; OPA started with token authentication (no token, a wrong
  token and a policy replacement refused, the right token answered); a real MCP 1.30 tool server
  over mutual TLS (the gateway's call answered; no certificate, plain HTTP and a forged certificate
  refused). Certificates carry the extensions Python 3.13's strict checking requires.
- Checkpoints on one machine are evidence, not a guarantee: whoever can delete Kafka events can
  delete the checkpoint files too. A deployment sets `AUDIT_CHECKPOINT_DIR` to storage with a
  retention lock. After resetting Kafka on purpose, archive `logs/audit-checkpoints/`, or the
  verifier reports the old chains as deleted.
- mTLS is opt-in (`GOVERNED_MTLS=on`) until it has run on more machines; the OPA token is on by
  default. Kafka itself (SC-3: SASL, TLS, ACLs) is still open and is best done with the real hosting.
- `/health` needs `GOVERNED_HEALTH_REQUIRE` adjusted where no planner runs (a profile served only
  through the governed endpoint): empty, or the workers that do run.
