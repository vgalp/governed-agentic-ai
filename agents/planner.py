"""Planner agent: input guardrails, mask, retrieve approved content through the
gateway, ask the model, output guardrails, restore details, publish.

Guardrails run in layers: fast rules (layer 1), then a safety classifier (layer 2)
for what the rules miss. Questions go to the profile's input engine (the fine-tuned guard
classifier, or Llama Guard); answers always go to Llama Guard. If a rule blocks a question
for another reason and the classifier sees a crisis, the crisis reply wins. On answers,
profiles that cite sources also get a grounding check: an answer may only cite sources
the model was given.

Everything specific to the use case (model, prompt, rules, replies, classifier
policy, privacy settings, knowledge base, tool permissions, roles) comes from the
active profile: PROFILE=<name>, default adhd-assistant. See profiles/.

In a profile with roles, the requesting role decides which tools may be used and
which record data is fetched (OPA). The role is checked after the input guardrails,
so a crisis message is answered with the crisis reply even without a role.

Every step is also published to agent.trace as it happens (agents/trace.py): a readable
decision trace for the dashboard, with no personal data.

The pipeline itself is agents/pipeline: stages that each return an outcome or pass the request
on, output checks as a list, and a Runtime holding the I/O so the whole thing runs under test.
This file is the process around it: Kafka in, one request at a time, Kafka out.

A request for a change (profiles with actions, e.g. "move this appointment") is not
answered: the planner prepares a proposal for a person to approve (agents/actions.py).
It cannot make the change; only the executor agent can, after approval."""

import json

from confluent_kafka import Consumer

from agents.pipeline import build, handle
from agents.pipeline.stages import fail
from audit.chain import get_audit_chain
from guardrails import toxicity
from guardrails.classifier import input_engine_name, load_guard
from ops.delivery import Producer
from ops.health import Heartbeat
from ops.kafka import describe, kafka_config
from profiles.loader import load_profile

AGENT_NAME = "planner"


def main() -> None:
    profile = load_profile()          # fails here, at startup, if the profile is incomplete
    if profile.classifier.input_engine == "guard_model":
        try:                          # load it now, so a missing model stops startup, not a request
            load_guard(profile)
        except (ImportError, SystemExit) as e:
            raise SystemExit(f"Guard classifier {profile.classifier.guard_model} could not be loaded ({e}). "
                             "Install it with: uv sync --group train, train it, or set the profile's "
                             "classifier input_engine to llamaguard.") from e
    toxicity.load()                   # now, so the first answer is not slowed by loading it
    consumer = Consumer(kafka_config(AGENT_NAME, {"group.id": "planner-agent",
                         "auto.offset.reset": "earliest", "enable.auto.commit": False,
                         # one request can take minutes on a laptop (several model calls); without this the
                         # broker evicts the planner after 5 minutes and the commit fails (audit OC-1)
                         "max.poll.interval.ms": 900_000}))
    producer = Producer(service=AGENT_NAME)   # delivery confirmed (ADR 030); TLS and SASL (ADR 031)
    print(describe(kafka_config(AGENT_NAME)), flush=True)
    consumer.subscribe(["chat.requests"])
    rt = build(profile, producer, get_audit_chain(AGENT_NAME), AGENT_NAME)
    ext = (" -> ".join(f"{profile.models[k]['provider']}/{profile.models[k]['name']}"
                       for k in [profile.routing["external_model"], *profile.routing["fallback"]])
           if profile.routing["external_allowed"] else "none")
    print(f"Planner agent listening on chat.requests (profile {profile.name}, "
          f"local model {profile.local_model['name']}, external model {ext}, "
          f"question guard {input_engine_name(profile)}, answer guard {profile.classifier.model}, "
          f"input agent {rt.guards['input_agent'] or 'off'}, toxicity {toxicity.status()})...", flush=True)
    hb = Heartbeat(AGENT_NAME)
    while True:
        hb.beat()
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            print("Kafka error:", msg.error(), flush=True)
            continue
        hb.working("a chat request")        # a long request is not a dead planner (ops/health.py)
        process(rt, msg.value())
        try:
            consumer.commit(message=msg)
        except Exception as e:        # noqa: BLE001  a failed commit means the message is seen again, not a dead planner
            print(f"Commit failed ({type(e).__name__}); the request may be processed again", flush=True)


def process(rt, value: bytes) -> str:
    """One chat.requests message, never raising: a message that is not a request is skipped (and
    committed by the caller, so it cannot crash the planner on every restart); a request that
    raises gets the fixed reply and an audit entry. Returns what happened, for tests and logs."""
    try:
        raw = json.loads(value)
    except (ValueError, TypeError):
        print("Skipped a chat.requests message that is not JSON", flush=True)
        return "skipped"
    if not isinstance(raw, dict) or not isinstance(raw.get("request_id"), str) or not isinstance(raw.get("message"), str):
        print("Skipped a chat.requests message without a request_id and message", flush=True)
        return "skipped"
    try:
        handle(rt, raw)
        return "handled"
    except Exception as e:            # noqa: BLE001  one bad request must not stop the service
        try:
            fail(rt, raw, e)          # the person gets a reply, the audit gets the failure
        except Exception as e2:       # noqa: BLE001
            print(f"Request failed ({type(e).__name__}) and could not reply ({type(e2).__name__})", flush=True)
        return "failed"


if __name__ == "__main__":
    main()
