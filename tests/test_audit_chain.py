"""Tamper-evident audit chain. Uses a fake producer, so no Kafka is needed."""

import json

from audit.chain import AuditChain, verify_chain

KEY = b"test-key"


class FakeProducer:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key=None, value=None):
        self.sent.append(json.loads(value))

    def flush(self):
        pass


def make_chain(n=5, key=KEY):
    producer = FakeProducer()
    chain = AuditChain(producer, "planner", key)
    for i in range(n):
        chain.emit("audit.guardrails", {"request_id": f"r{i}", "allowed": True})
    return producer.sent


def test_untouched_chain_is_valid():
    assert verify_chain(make_chain(), KEY) == []


def test_changed_event_is_detected():
    records = make_chain()
    records[2]["allowed"] = False
    assert any("seq 2" in p and "does not match" in p for p in verify_chain(records, KEY))


def test_deleted_event_is_detected():
    records = make_chain()
    del records[2]
    assert any("expected seq 2" in p for p in verify_chain(records, KEY))


def test_reordered_events_are_detected():
    records = make_chain()
    records[1]["audit"]["seq"], records[2]["audit"]["seq"] = 2, 1
    assert verify_chain(records, KEY)


def test_event_moved_to_another_topic_is_detected():
    records = make_chain()
    records[0]["audit"]["topic"] = "audit.tool_calls"
    assert verify_chain(records, KEY)


def test_rewritten_chain_without_the_key_is_detected():
    # An attacker changes an event and recomputes every hash, but without the key.
    forged = make_chain(key=None)
    assert verify_chain(forged, KEY)


def test_chains_from_two_processes_are_checked_separately():
    assert verify_chain(make_chain(3) + make_chain(4), KEY) == []