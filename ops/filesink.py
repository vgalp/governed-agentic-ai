"""The audit trail in files instead of Kafka, for a deployment without a broker (ADR 039).

The standalone gateway is the governed endpoint, OPA and the record tool servers. Asking a hospital
to run Kafka for it would be the largest part of the install, so the audit chain can write to files:

    AUDIT_SINK=file AUDIT_DIR=/var/lib/governed/audit python -m gateway.endpoint

`FileProducer` has the interface of ops.delivery.Producer (produce, flush, poll, send, list_topics),
so nothing that writes an event knows the difference.

- **Only audit topics are written**, one file per topic (audit.tool_calls.jsonl), one event per line,
  flushed to disk before the delivery is confirmed. The chain, its hashes and its checkpoints are the
  same as with Kafka; `python -m audit.verify --dir <AUDIT_DIR>` checks them.
- **Everything else is dropped**: the trace and chat topics carry message text, are kept only briefly
  on Kafka, and have no reader in a deployment without the dashboard.
- **A write that fails is counted as a lost audit event** (ops.delivery.DELIVERY), exactly as a Kafka
  delivery that fails.

One machine, one writer process per file. Files are created readable by their owner only. Put AUDIT_DIR
on storage the gateway's account can append to and nobody can rewrite (a write-once volume, or shipped
by a log collector): on an ordinary disk the hash chain shows tampering, it does not prevent it.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from audit.chain import AUDIT_TOPICS
from ops.delivery import DELIVERY, Delivery

DEFAULT_DIR = "logs/audit"


class FileProducer:
    """Appends audit events to <directory>/<topic>.jsonl. Thread-safe within one process."""

    def __init__(self, directory: Path, delivery: Delivery = DELIVERY, topics: tuple[str, ...] = tuple(AUDIT_TOPICS)):
        self.directory, self.delivery, self.topics = Path(directory), delivery, set(topics)
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def produce(self, topic, key=None, value=None, on_delivery=None, **kw) -> None:
        if topic not in self.topics:
            if on_delivery is not None:
                on_delivery(None, None)          # not kept, by design: nothing was lost
            return
        line = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        error: OSError | None = None
        try:
            with self._lock:
                fd = os.open(self.directory / f"{topic}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.replace("\n", " ").encode("utf-8") + b"\n")
                    os.fsync(fd)                 # on disk before the event counts as delivered
                finally:
                    os.close(fd)
        except OSError as e:
            error = e
        self.delivery.record(topic, error)
        if on_delivery is not None:
            on_delivery(error, None)

    def send(self, topic, key=None, value=None, timeout: float = 10) -> bool:
        """Write one event and say whether it is on disk."""
        result: dict = {}
        self.produce(topic, key=key, value=value, on_delivery=lambda err, msg: result.setdefault("err", err))
        return result.get("err") is None

    def flush(self, timeout: float = 10) -> int:
        return 0                                 # every event is flushed as it is written

    def poll(self, timeout: float = 0) -> int:
        return 0

    def list_topics(self, *a, **k):
        return None


def read_events(directory: Path) -> list[dict]:
    """Every event in a file sink's directory, for the verifier. A line that is not JSON is returned as
    {"unreadable": ...} so the verifier counts it instead of skipping it."""
    import json

    events: list[dict] = []
    for path in sorted(Path(directory).glob("audit.*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"unreadable": f"{path.name} line {n}"})
    return events
