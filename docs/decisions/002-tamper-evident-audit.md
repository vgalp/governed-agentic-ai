# 002: Tamper-evident audit log

Status: Accepted · Date: 2026-10-05

## Context
Audit events (guardrail decisions, tool calls, model inputs) were plain Kafka messages. Anyone with
access to the broker could change or delete one without trace. Regulated users need to show that
the record of what an AI system did has not been altered.

## Decision
- Every audit event carries a chain id (one per process), a sequence number, the previous event's
  hash, the topic, and its own hash over all of that (`audit/chain.py`).
- With `AUDIT_HMAC_KEY` set, hashes are HMAC-SHA256; otherwise SHA-256. Each event records which.
- `python -m audit.verify` and the dashboard's `/audit/verify` check every chain and report
  changed, missing, reordered or moved events.

## Consequences
- Changes, deletions, insertions and reordering are detected.
- Without the key, someone who rewrites the whole log can recompute a valid SHA-256 chain; such
  chains are reported as "unkeyed".
- **Truncation (deleting the newest events) is not detected.** Next step: periodically publish the
  latest hash to a separate, append-only location (a checkpoint).
- One chain per process; a restarted process starts a new chain, which is expected.
