"""Kafka delivery that is confirmed, not assumed (audit OC-2, ADR 030).

Every message is produced with a delivery callback. A message the broker did not take is counted,
logged with its topic and the error (never its content), and shown by /health. A lost audit event
is reported as such: the chain now has a gap that the verifier will find.

Where a person is waiting (a chat message, an approval decision) the API uses `send()`, which waits
for the broker's confirmation and returns False if there was none, so the person is told to try
again instead of being given a request ID that nothing will ever answer.

The wrapper keeps confluent_kafka's produce / flush / poll / list_topics interface, so code and
tests that use a fake producer are unchanged.
"""

import os
import threading
import time
from collections import deque

from audit.chain import AUDIT_TOPICS

# No duplicates when a send is retried. KAFKA_IDEMPOTENCE=0 only for a broker without support for it.
IDEMPOTENT = os.environ.get("KAFKA_IDEMPOTENCE", "1") != "0"

RECENT_SECONDS = 300          # a failure this recent makes /health report "degraded"


class Delivery:
    """Counts confirmed and failed deliveries for one process."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.delivered = 0
        self.failed = 0
        self.audit_lost = 0
        self.unconfirmed = 0               # still queued when a flush timed out
        self.recent: deque = deque(maxlen=20)
        self._lock = threading.Lock()

    def record(self, topic: str, error) -> None:
        with self._lock:
            if error is None:
                self.delivered += 1
                return
            self.failed += 1
            if topic in AUDIT_TOPICS:
                self.audit_lost += 1
            self.recent.append({"at": self.clock(), "topic": topic, "error": str(error)[:200]})
        lost = " AUDIT EVENT LOST: the audit chain now has a gap." if topic in AUDIT_TOPICS else ""
        print(f"Kafka delivery failed on {topic}: {str(error)[:200]}.{lost}", flush=True)

    def timed_out(self, left: int) -> None:
        with self._lock:
            self.unconfirmed += left
            self.recent.append({"at": self.clock(), "topic": "*", "error": f"{left} message(s) unconfirmed after flush"})
        print(f"Kafka: {left} message(s) not confirmed when the flush timed out", flush=True)

    def stats(self) -> dict:
        now = self.clock()
        with self._lock:
            recent = [f for f in self.recent if now - f["at"] <= RECENT_SECONDS]
            return {"delivered": self.delivered, "failed": self.failed, "audit_lost": self.audit_lost,
                    "unconfirmed": self.unconfirmed, "recent_failures": len(recent),
                    "last_failure": dict(self.recent[-1]) if self.recent else None}


DELIVERY = Delivery()          # one per process: every producer in it reports here


class Producer:
    """confluent_kafka.Producer with delivery callbacks on every message."""

    def __init__(self, conf: dict | None = None, delivery: Delivery = DELIVERY, raw=None, service: str = "api"):
        if raw is None:
            from confluent_kafka import Producer as KafkaProducer

            from ops.kafka import kafka_config  # TLS and SASL from the environment (ADR 031)
            raw = KafkaProducer(kafka_config(service, {"enable.idempotence": IDEMPOTENT, "acks": "all", **(conf or {})}))
        self._p, self.delivery = raw, delivery

    def produce(self, topic, key=None, value=None, on_delivery=None, **kw):
        def done(err, msg):
            self.delivery.record(topic, err)
            if on_delivery is not None:
                on_delivery(err, msg)
        self._p.produce(topic, key=key, value=value, on_delivery=done, **kw)
        self._p.poll(0)                    # serve callbacks of earlier messages

    def flush(self, timeout: float = 10) -> int:
        left = self._p.flush(timeout)
        if left:
            self.delivery.timed_out(left)
        return left

    def poll(self, timeout: float = 0):
        return self._p.poll(timeout)

    def list_topics(self, *a, **k):
        return self._p.list_topics(*a, **k)

    def send(self, topic, key=None, value=None, timeout: float = 10) -> bool:
        """Produce one message and wait for the broker to confirm it. False: not confirmed."""
        result = {}
        self.produce(topic, key=key, value=value, on_delivery=lambda err, msg: result.setdefault("err", err))
        self.flush(timeout)
        return "err" in result and result["err"] is None
