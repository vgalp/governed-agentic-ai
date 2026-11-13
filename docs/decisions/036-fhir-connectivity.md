# 036: Production connectivity to FHIR servers

Status: Accepted · Date: 2026-11-13 · Applies to: Epic, Oracle Health (and MEDITECH, unchanged by default)

## Context
ADR 013 and ADR 032 made the connectors correct: the right sign-in, the right searches, waiting
when a server is busy, signing in again once on 401, saying when a list is cut off. A strict
review for commercial use found that they were not yet *operable*: nothing bounded how much load
the assistant puts on an EHR, nothing stopped a dead server from making every question wait for
its timeouts, nothing showed what the connectors were doing, and a malformed answer could reach the
records as an empty list. Launched sessions (ADR 035) lived in files on one machine.

## Decision
All requests to one FHIR server in a process go through one `Transport` (`fhir/transport.py`),
shared by the backend client and by the client built for each call launched from the chart. The
client (`fhir/client.py`) keeps what needs the sign-in or FHIR: the token, 401, Retry-After,
paging, validation. Its public API is unchanged.

1. **Circuit breaker** (`fhir/resilience.py`). After `breaker.failures` consecutive failures
   (timeouts, lost connections, 500/502/503/504) the server is not called for
   `breaker.reset_seconds`; questions get "<host> is not answering" at once. Then one trial call is
   let through; its outcome closes or reopens the breaker. 4xx and 429 do not count: the server
   answered.
2. **Pooling and concurrency.** A pooled `requests.Session` (`pool_size` kept-alive connections,
   urllib3 retries off: the client decides what is retried) and at most `max_concurrent` requests
   in flight per server per process. A request that cannot get a slot within its budget fails.
3. **Client-side rate limit.** A token bucket (`rate_limit: {per_second, burst}`): bursts, such as
   the chart brief's parallel reads, stay under the EHR's limit instead of meeting its 429s.
4. **Metrics** (`fhir/metrics.py`), Prometheus text at `GET /metrics` on every tool server:
   attempts by outcome, latency histograms, retries by reason, refusals before sending (breaker,
   rate, deadline, queue), breaker state, cache hits, invalid answers. Labels are the host, the
   resource type (checked against a pattern, so never an id) and the outcome; no patient data.
   Under mutual TLS (ADR 030) only a client with the gateway's CA can read it.
5. **Response validation** (`fhir/validation.py`). A read must return that type and id; a search a
   `searchset` Bundle whose matches are all the type searched for; JSON, or a FHIR JSON content
   type. Included resources are skipped; OperationOutcome entries are allowed, and an error-level
   one marks the list as possibly incomplete (the brief then says so, as for a cut-off list).
   Anything else is a `FhirResponseError`, counted in the metrics.
6. **Time budget per question.** Each tool call runs within `budget` seconds (default 75, set
   below the tool's own timeout in tools.yaml, validated to be longer than one request's
   `timeout`). It is a context variable, copied into the chart brief's threads. Every timeout, queue
   wait, rate-limit wait and Retry-After is cut to what is left; a wait longer than that is not
   made. When the budget runs out, the record types not yet read are reported as not read.
7. **Per-session cache** (`fhir/cache.py`), `cache_seconds` (at most 120). Only for a person's own
   sign-in (a launched clinician, or `smart_user`): entries are scoped to the session and never
   served to another. The backend identity is never cached, so the hospital's audit log records
   each access when it happens. Only validated 200 answers; a 401 drops the session's entries.
8. **Capability check** (`fhir/capabilities.py`). At startup (records and chart-brief servers) and
   in `scripts/fhir_check.py`, the CapabilityStatement is compared with the mapping: a resource
   type not offered is an error; a search parameter not declared is a warning, because a FHIR
   server may ignore a parameter it does not support and return *more* (an undated window). The
   check reports; it never drops a filter.
9. **Recorded answers in tests** (`fhir/recording.py`, `scripts/fhir_record.py`,
   `tests/fixtures/fhir/<vendor>/`). The recorder makes the assistant's own requests against a
   vendor sandbox and writes each answer (base URL replaced by a placeholder, no headers but
   Retry-After, no tokens); `ReplaySession` serves them to the unchanged client. A request not in
   the recording fails the test. Recording refuses any host that is not a known sandbox, because
   recordings are committed. The first Epic and Oracle Health folders are **hand-built** in the
   recording format from what the sandboxes sent on 2026-11-12 (invented patients) and say so in
   `index.json`; running the recorder replaces them.
10. **Shared session store** (`fhir/sessions.py`). Sessions are encrypted (Fernet) before storage as
    before; where they are stored is a `BlobStore`: memory, a folder (default), or Redis
    (`GAAI_SESSIONS_URL`, or a secret reference to it) with the key expiring at the session's end.
    Plain `redis://` is refused for any host but this machine. Keys are the SHA-256 of the
    session ID. The `redis` package is in the `deploy` dependency group.

Settings (all optional, in `fhir.yaml` under `server:`, validated with bounds in
`profiles/fhir_map.py`): `retries`, `max_concurrent`, `pool_size`, `rate_limit`, `breaker`,
`budget`, `cache_seconds`. Epic and Oracle Health set them explicitly; MEDITECH uses the defaults
(no rate limit, no cache).

## Consequences
- An EHR outage costs one timeout per question until the breaker opens, then nothing; the
  operator sees it in `fhir_breaker_open` and the retries counters.
- The load the assistant can put on an EHR is bounded per tool server: `max_concurrent` and
  `rate_limit` multiply by the number of tool-server processes, which a hospital's integration team
  should know when agreeing limits.
- Backoff without Retry-After is jittered (half fixed, half random), so clients that failed together
  do not retry together. Retry-After is honoured exactly, up to 10 seconds.
- A timeout or lost connection is retried at once (GET is safe to repeat); the breaker, not a wait,
  handles a sustained outage.
- Deploying with Redis needs `uv lock` once (the new `redis` dependency) before the container build.
- Not done here: per-tenant limits across processes (needs a shared limiter), OpenTelemetry traces,
  and an Inferno (US Core) run, which remain on the production list.
