# 039: The governed gateway by itself

Status: Accepted · Date: 2026-11-14 · Runs on one machine from scripts; not packaged as its own container, not run in a hospital

## Context
The part of this project a hospital can adopt first is not the assistant. It is the layer between an
AI assistant the hospital already has and its record system: who may call, what they may see, what
may leave, and a record of all of it. ADR 015 built that layer as a governed MCP endpoint, but it
could only be run as part of the whole pipeline:

- no shipped profile turned it on;
- its audit trail needed Kafka, which is the largest part of installing the pipeline;
- there was no way to try it without an identity system to answer "who is this token";
- nothing said how to start it or what to tell a hospital's IT team.

## Decision
Make the endpoint runnable by itself: OPA, the record tool servers and the endpoint.

1. **The FHIR profiles offer their record tools to outside assistants** (`outside.yaml`,
   `agents.outside`) in clinic-epic, clinic-oracle and clinic-meditech: record search, medication
   list, medication review and chart brief. Read-only. Everything the gateway already enforces
   applies: tools and data classes by role (ADR 005), sensitive categories by role (ADR 038), no
   record data to a client at an outside AI service, input checks on the request, audit.
2. **The audit trail can go to files** (`AUDIT_SINK=file`, `ops/filesink.py`). One file per audit
   topic, one event per line, flushed to disk before it counts as delivered. The chain, the hashes
   and the checkpoints are unchanged; `python -m audit.verify --dir` checks them and reports a
   damaged line. Trace and chat events, which carry message text, are not written. A failed write is
   a lost audit event, counted as a failed Kafka delivery is. An unknown `AUDIT_SINK` stops the
   process.
3. **A development-only stand-in for the identity system** (`ops/dev_identity.py`): answers the
   gateway's token check from a local file that holds token hashes, on 127.0.0.1 only (port 8120),
   behind the shared secret. It exists so the gateway can be tried; it is not a way to run it.
4. **One script starts it** (`scripts/gateway.sh`), **one script calls it as an assistant would**
   (`scripts/gateway_check.py`), and **one guide** explains it (`docs/gateway/README.md`).

## Consequences
- **No Kafka, no AI model and no web page are needed** to put the record tools behind policy. The
  guard classifier still runs on each request, so torch and the guard's weights are needed.
- **The default trial uses Oracle Health's open sandbox**, which needs no credentials.
- **The file audit trail is for one process on one machine.** It shows tampering and does not prevent
  it. A deployment with more than one gateway, or that needs the audit kept apart from the machine
  that writes it, uses Kafka (ADR 031) or ships the files to write-once storage.
- **Callers are identified by a token check only.** An identity provider without one (Microsoft Entra
  ID) needs an adapter or JWT validation, which is not built.
- **The gateway listens on plain HTTP on 127.0.0.1.** Reaching it from another machine needs a
  reverse proxy with TLS, which is not provided.
- **`scripts/gateway.sh` and `scripts/gateway_check.py` were written without running them end to
  end:** the workspace they were written in has no Docker, no OPA and no route to the sandboxes. The
  parts they start are tested separately. Their first run on a developer's machine is the test.
- Not built: a container image for the gateway alone; a rate limit per caller; health and metrics on
  the gateway; JWT validation; TLS.
