"""Tamper-evident audit chain. Uses a fake producer, so no Kafka is needed."""

import json

from audit.chain import AuditChain, unkeyed_chains, verify_chain

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


def test_chain_rewritten_without_the_key_is_flagged_as_unkeyed():
    # An attacker rewrites the log and recomputes every hash, but has no key,
    # so the result can only be a plain SHA-256 chain, and it is reported as such.
    forged = make_chain(key=None)
    assert unkeyed_chains(forged) == [forged[0]["audit"]["chain_id"]]
    assert unkeyed_chains(make_chain()) == []


def test_keyed_chain_with_wrong_key_is_detected():
    assert verify_chain(make_chain(), b"wrong-key")


def test_keyed_chain_cannot_be_verified_without_a_key():
    assert any("AUDIT_HMAC_KEY is not set" in p for p in verify_chain(make_chain(), None))


def test_chains_from_two_processes_are_checked_separately():
    assert verify_chain(make_chain(3) + make_chain(4), KEY) == []