# 025: The planner as stages, with the I/O in one object

Status: Accepted · Date: 2026-11-02

## Context
`agents/planner.py` had grown to a 400-line loop body that did everything: input checks, roles,
masking, tools, proposals, routing, the model, output checks, publishing. Every feature of the
past month was a patch inside it; `continue` was its control flow; it connected to Kafka and loaded
models at import, so nothing in it could be tested without the stack. Eight hundred tests, none of
the file that runs the request. An architect reading the repository opens that file first.

## Decision
- **`agents/pipeline/`**: `Request` (the state of one request, filled in stage by stage),
  `Outcome` (how it ended: reply, sources, the deciding check, the log line), `Runtime` (the
  producer, the audit chain, the model client, the gateway call, the masker, the policy engine),
  `stages.py` (one function per stage, in a list; each returns an Outcome or None) and `checks.py`
  (the output checks as a list, each marked with its layer).
- **`agents/planner.py`** is the process: connect, loop, `handle(runtime, message)`, commit. A
  request that raises gets the fixed "unavailable" reply and an audit entry (`pipeline_error`);
  the service keeps running.
- **`privacy/tokens.py`** holds the pure token helpers (`restore_types`, `unmask`), so the pipeline
  imports nothing that loads Presidio; `privacy.masking` re-exports them.
- **`tests/test_pipeline.py`** runs the whole pipeline with fakes: a crisis stopped before any
  model, a plain answer with notes, masking round-trip, an invented citation blocked, a research
  question with nothing read, the judge in warn and withhold modes, no model available, roles,
  a direct tool, a tool the role may not use, a failing tool, a change proposal, a pipeline error.
- **ruff** in CI (`pyproject.toml`: E/F/W/I/B at 130 columns; prompts and test fixtures exempt
  from line length). The repository lints clean.

Behaviour is unchanged: the same checks in the same order, the same trace steps, the same audit
events, the same replies. The existing tests pass untouched.

## Consequences
- Adding an output check is one function and one list entry; adding a stage is one function and
  one list entry; both are visible in `docs/architecture.md`.
- The pipeline is testable without Kafka, OPA, Ollama or Presidio, which is where the next
  changes (a second judge, an intent classifier for triggers, an in-memory bus) get their tests.
- Still to do, in the same spirit: pydantic models for the profile files (the loader is a
  thousand lines of hand-written validation), a shared records-access module for the three
  direct-answer tools, a service registry instead of per-profile `127.0.0.1` addresses, and
  structured logging in place of `print`.
