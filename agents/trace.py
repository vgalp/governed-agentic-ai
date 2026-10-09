"""Decision trace: a readable, step-by-step account of how each request was handled.

Every step a request goes through (input checks, role check, masking, each tool call,
routing, the model call, output checks, the reply) is published to the agent.trace
topic as it happens, so the dashboard can show it live and a past request can be
replayed from Kafka.

The trace records the pipeline's decisions and the rule or policy behind each one.
It is not the model's internal reasoning, which is neither checked nor a reliable
explanation of an answer.

Privacy: like the audit, the trace never holds personal data. No messages, answers or
names; only categories, counts, kinds of masked details and source IDs.

The audit topics remain the record of truth (tamper-evident, see audit/chain.py).
The trace is the readable view of the same decisions.
"""

import json
import re
import time
from collections import Counter

TRACE_TOPIC = "agent.trace"

# Which agent owns each step. Today the planner runs the first five; in the multi-agent
# pipeline each becomes its own agent, and the trace keeps these names. "action" holds a
# proposed change, its approval and its execution (planner, then the executor agent).
# A tool's steps show under the lane set for it in the profile's tools.yaml.
AGENTS = ("intake", "knowledge", "records", "router", "answer", "action")

# running: a slow step has started (classifier, tool call, model)
# pass / block: a check allowed or stopped the request
# override: a classifier flag was overridden by a narrow profile exemption
# info: a step that decides nothing by itself (masking, routing, the reply)
# error: something failed (the pipeline then fails safe)
OUTCOMES = ("running", "pass", "block", "override", "info", "error")

TOKEN_RE = re.compile(r"^\[([A-Z_]+)_\d+\]$")


def masking_summary(mapping: dict[str, str]) -> str:
    """Kinds and counts of masked details, never the values."""
    kinds = Counter(m.group(1) for t in mapping if (m := TOKEN_RE.match(t)))
    if not kinds:
        return "No personal details found"
    parts = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items()))
    return f"Masked {sum(kinds.values())} personal detail(s): {parts}"


def ids_text(ids: list[str], limit: int = 6) -> str:
    shown = ", ".join(ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


class Trace:
    """The trace of one request. Publishes each step to agent.trace as it happens."""

    def __init__(self, producer, profile: str, request_id: str, role: str | None = None,
                 process: str = "planner", started_at: float | None = None):
        self.producer = producer
        self.profile = profile
        self.request_id = request_id
        self.role = role
        self.process = process          # which running process did the step
        # Wall-clock start of the request, so steps from another process (the executor,
        # minutes later) line up on the same timeline.
        self.started_at = started_at if started_at is not None else time.time()
        self.seq = 0
        self._started: dict[tuple[str, str], float] = {}

    def start(self, agent: str, step: str, summary: str) -> dict:
        """Mark a slow step as running, so the dashboard shows it while it works."""
        self._started[(agent, step)] = time.monotonic()
        return self._emit(agent, step, "running", summary, {})

    def step(self, agent: str, step: str, outcome: str, summary: str, **details) -> dict:
        """Record the outcome of a step. details: small facts (category, rule, counts, IDs)."""
        return self._emit(agent, step, outcome, summary, details)

    def _emit(self, agent: str, step: str, outcome: str, summary: str, details: dict) -> dict:
        if agent not in AGENTS:
            raise ValueError(f"unknown agent {agent!r}")
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        now = time.monotonic()
        started = self._started.pop((agent, step), None) if outcome != "running" else None
        event = {
            "request_id": self.request_id,
            "profile": self.profile,
            "role": self.role,
            "process": self.process,
            "agent": agent,
            "step": step,
            "outcome": outcome,
            "summary": summary,
            "details": details,
            "seq": self.seq,                                   # order within this process's steps
            "elapsed_ms": round((time.time() - self.started_at) * 1000),   # since the request was picked up
            "duration_ms": round((now - started) * 1000) if started is not None else None,
            "timestamp": time.time(),
        }
        self.seq += 1
        self.producer.produce(TRACE_TOPIC, key=self.request_id, value=json.dumps(event))
        self.producer.poll(0)       # send now, without waiting: the trace is live
        return event