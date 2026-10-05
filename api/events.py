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

from audit.chain import AUDIT_TOPICS

EVENT_TOPICS = ["chat.requests", "chat.responses", *AUDIT_TOPICS]


def sanitize(topic: str, value: dict) -> dict:
    """Keep only what an operator may see."""
    if topic == "chat.requests":
        return {"request_id": value.get("request_id"), "message_chars": len(value.get("message", ""))}
    if topic == "chat.responses":
        return {
            "request_id": value.get("request_id"),
            "guardrail": value.get("guardrail"),
            "sources": [s.get("id") for s in value.get("sources", [])],
            "answer_chars": len(value.get("answer", "")),
        }
    return value  # audit events already hold masked text and decisions only


class EventBuffer:
    """Recent events for the live feed, plus every chained audit record for verification."""

    def __init__(self, maxlen: int = 2000):
        self.maxlen = maxlen
        self.events: list[dict] = []
        self.next_id = 1
        self.audit_records: list[dict] = []
        self.lock = threading.Lock()

    def add(self, topic: str, value: dict) -> None:
        with self.lock:
            self.events.append({"id": self.next_id, "topic": topic, "received_at": time.time(),
                                "data": sanitize(topic, value)})
            self.next_id += 1
            if len(self.events) > self.maxlen:
                self.events = self.events[-self.maxlen:]
            if "audit" in value:
                self.audit_records.append(value)

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
            buffer.add(msg.topic(), json.loads(msg.value()))
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