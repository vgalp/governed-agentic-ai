"""Tamper-evident audit log.

Every audit event gets a sequence number and a hash that covers the event
itself and the hash of the event before it. Changing, deleting, inserting or
reordering any event breaks the chain, and `verify_chain` reports where.

If AUDIT_HMAC_KEY is set, hashes are HMAC-SHA256 with that key, so someone who
can edit the log but does not hold the key cannot recompute a valid chain.
Without a key, plain SHA-256 still detects accidental or careless changes.

Each event records which algorithm signed it ("hmac-sha256" or "sha256"), so
chains written before a key was configured can still be checked, and are
reported as unkeyed rather than as tampered.

Known limit: deleting the most recent events (truncation) leaves a valid chain.
Publishing the latest hash somewhere separate (a checkpoint) closes that gap;
see docs/decisions/002-tamper-evident-audit.md.
"""

import hashlib
import hmac
import json
import os
import threading
import time
import uuid

GENESIS = "0" * 64
AUDIT_TOPICS = ["audit.guardrails", "audit.model_inputs", "audit.tool_calls"]


def _canonical(record: dict) -> bytes:
    """Same JSON bytes for the same content, whatever the key order."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_hash(record: dict, key: bytes | None) -> str:
    """Hash a record that does not yet contain its own hash."""
    data = _canonical(record)
    if key:
        return hmac.new(key, data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


def key_from_env() -> bytes | None:
    value = os.environ.get("AUDIT_HMAC_KEY")
    return value.encode() if value else None


class AuditChain:
    """Writes chained audit events. One chain per running process."""

    def __init__(self, producer, source: str, key: bytes | None = None):
        self.producer = producer
        self.chain_id = f"{source}-{uuid.uuid4()}"
        self.key = key
        self.seq = 0
        self.prev_hash = GENESIS
        self._lock = threading.Lock()

    def emit(self, topic: str, event: dict, flush: bool = False) -> dict:
        with self._lock:
            record = {
                **event,
                "timestamp": event.get("timestamp", time.time()),
                "audit": {
                    "chain_id": self.chain_id,
                    "seq": self.seq,
                    "topic": topic,               # moving an event to another topic is detected too
                    "prev_hash": self.prev_hash,
                    "alg": "hmac-sha256" if self.key else "sha256",
                },
            }
            record["audit"]["hash"] = compute_hash(record, self.key)
            self.producer.produce(topic, key=event.get("request_id"), value=json.dumps(record))
            self.prev_hash = record["audit"]["hash"]
            self.seq += 1
        if flush:
            self.producer.flush()
        return record


def verify_chain(records: list[dict], key: bytes | None) -> list[str]:
    """Check chained audit records. Returns a list of problems; empty means intact.

    Unkeyed chains are checked with plain SHA-256; see `unkeyed_chains` to list them.
    """
    problems = []
    chains: dict[str, list[dict]] = {}
    for r in records:
        chains.setdefault(r["audit"]["chain_id"], []).append(r)

    for chain_id, entries in chains.items():
        entries.sort(key=lambda r: r["audit"]["seq"])
        expected_seq, expected_prev = 0, GENESIS
        for r in entries:
            a = r["audit"]
            if a["seq"] != expected_seq:
                problems.append(f"{chain_id}: expected seq {expected_seq}, found {a['seq']} (event missing or duplicated)")
            if a["prev_hash"] != expected_prev:
                problems.append(f"{chain_id} seq {a['seq']}: does not link to the previous event")
            unhashed = {**r, "audit": {k: v for k, v in a.items() if k != "hash"}}
            keyed = a.get("alg") == "hmac-sha256"
            if keyed and key is None:
                problems.append(f"{chain_id} seq {a['seq']}: signed with a key, but AUDIT_HMAC_KEY is not set here")
            elif compute_hash(unhashed, key if keyed else None) != a["hash"]:
                problems.append(f"{chain_id} seq {a['seq']}: content does not match its hash (changed after writing)")
            expected_seq, expected_prev = a["seq"] + 1, a["hash"]
    return problems


def unkeyed_chains(records: list[dict]) -> list[str]:
    """Chains written without a key: changes are detected, deliberate forgery is not."""
    return sorted({r["audit"]["chain_id"] for r in records if r["audit"].get("alg") != "hmac-sha256"})


_default_chain: AuditChain | None = None
_default_lock = threading.Lock()


def get_audit_chain(source: str) -> AuditChain:
    """The process-wide chain, so the planner and the gateway share one sequence."""
    global _default_chain
    with _default_lock:
        if _default_chain is None:
            from confluent_kafka import Producer
            producer = Producer({"bootstrap.servers": "localhost:9092", "enable.idempotence": True})
            key = key_from_env()
            if key is None:
                print("Audit: AUDIT_HMAC_KEY not set, using plain SHA-256 (detects changes, not deliberate forgery).")
            _default_chain = AuditChain(producer, source, key)
        return _default_chain