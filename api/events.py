"""Live event feed for the pipeline dashboard.

A background consumer reads every pipeline topic and keeps a short in-memory
buffer that browsers follow over Server-Sent Events (/events).

Privacy rule: the dashboard never shows personal data. Raw user messages and
restored answers are dropped here; only masked text, decisions and counts are
passed on.
"""

import asyncio
import json
import threading
import time
import uuid

from agents.actions import APPROVAL_TOPICS
from agents.trace import TRACE_TOPIC
from audit.chain import AUDIT_TOPICS

EVENT_TOPICS = ["chat.requests", "chat.responses", *AUDIT_TOPICS, TRACE_TOPIC, *APPROVAL_TOPICS]


def sanitize(topic: str, value: dict) -> dict:
    """Keep only what an operator may see."""
    if topic == "chat.requests":
        return {"request_id": value.get("request_id"), "role": value.get("role"), "profile": value.get("profile"),
                "message_chars": len(value.get("message", ""))}
    if topic == "chat.responses":
        return {
            "request_id": value.get("request_id"),
            "guardrail": value.get("guardrail"),
            "sources": [s.get("id") for s in value.get("sources", [])],
            "route": value.get("route"),
            "access": value.get("access"),
            "approval": value.get("approval"),
            "answer_chars": len(value.get("answer", "")),
            "profile": value.get("profile"),
            "metrics": value.get("metrics"),       # counts, tokens, cost, scores, model names: no text (ADR 018)
        }
    return value  # audit, trace and approval events already hold masked text, IDs and decisions only


class EventBuffer:
    """Recent events for the live feed, plus every chained audit record for verification."""

    def __init__(self, maxlen: int = 2000):
        self.maxlen = maxlen
        self.events: list[dict] = []
        self.next_id = 1
        self.audit_records: list[dict] = []
        self.lock = threading.Lock()
        self._seen: dict[tuple, int] = {}      # (topic, partition) -> last offset added

    def add(self, topic: str, value: dict, partition: int | None = None, offset: int | None = None) -> bool:
        """Add one message. A message already seen (same topic, partition and offset) is skipped:
        the consumer commits no offsets, so after a rebalance (for example when the laptop
        sleeps) Kafka hands it everything again from the start. Without this, every audit
        event would be counted again and look like a duplicated, tampered chain."""
        with self.lock:
            if offset is not None:
                key = (topic, partition)
                if offset <= self._seen.get(key, -1):
                    return False
                self._seen[key] = offset
            self.events.append({"id": self.next_id, "topic": topic, "received_at": time.time(),
                                "data": sanitize(topic, value)})
            self.next_id += 1
            if len(self.events) > self.maxlen:
                self.events = self.events[-self.maxlen:]
            if "audit" in value:
                self.audit_records.append(value)
            return True

    def since(self, last_id: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["id"] > last_id]


buffer = EventBuffer()


def consume_events(bootstrap: str) -> None:
    from confluent_kafka import Consumer
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": f"dashboard-{uuid.uuid4()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe(EVENT_TOPICS)
    while True:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        try:
            buffer.add(msg.topic(), json.loads(msg.value()), msg.partition(), msg.offset())
        except (ValueError, TypeError):
            continue


async def event_stream(last_id: int = 0):
    """Server-Sent Events: replay the buffer, then follow new events."""
    while True:
        for event in buffer.since(last_id):
            last_id = event["id"]
            yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
        yield ": keep-alive\n\n"
        await asyncio.sleep(0.5)