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


def test_a_rewritten_chain_relabelled_unkeyed_does_not_verify_where_a_key_is_set():
    """Audit finding SC-7: the verifier used to take the record's own `alg` on trust, so a chain
    rewritten without the key, relabelled "sha256" and rehashed, verified as intact."""
    original = make_chain()
    forged = make_chain(key=None)
    for r in forged:
        r["audit"]["chain_id"] = original[0]["audit"]["chain_id"]
    forged[1]["decision"] = "approve"                       # the forger's change
    rehashed, prev = [], "0" * 64
    from audit.chain import compute_hash
    for r in sorted(forged, key=lambda r: r["audit"]["seq"]):
        r["audit"]["prev_hash"] = prev
        r["audit"].pop("hash", None)
        r["audit"]["hash"] = compute_hash(r, None)
        prev = r["audit"]["hash"]
        rehashed.append(r)
    problems = verify_chain(rehashed, KEY)
    assert problems and all("not signed with the key" in p for p in problems)
    assert verify_chain(rehashed, KEY, allow_unkeyed=True) == []      # only on purpose, for old chains
    assert verify_chain(rehashed, None) == []                          # no key here: nothing to compare against


def test_a_chain_that_switches_algorithm_part_way_is_rewritten():
    keyed = make_chain()
    tail = make_chain(key=None)
    tail[-1]["audit"]["chain_id"] = keyed[0]["audit"]["chain_id"]
    tail[-1]["audit"]["seq"] = len(keyed)
    assert any("mixes signing algorithms" in p for p in verify_chain(keyed + [tail[-1]], KEY, allow_unkeyed=True))


def test_an_unknown_algorithm_is_a_problem():
    records = make_chain()
    records[0]["audit"]["alg"] = "none"
    assert any("unknown signing algorithm" in p for p in verify_chain(records, KEY))


def test_keyed_chain_with_wrong_key_is_detected():
    assert verify_chain(make_chain(), b"wrong-key")


def test_keyed_chain_cannot_be_verified_without_a_key():
    assert any("AUDIT_HMAC_KEY is not set" in p for p in verify_chain(make_chain(), None))


def test_chains_from_two_processes_are_checked_separately():
    assert verify_chain(make_chain(3) + make_chain(4), KEY) == []

def test_a_profile_view_keeps_whole_chains_of_that_profile():
    a, b = FakeProducer(), FakeProducer()
    ca, cb = AuditChain(a, "endpoint", KEY), AuditChain(b, "planner", KEY)
    for i in range(3):
        ca.emit("audit.tool_calls", {"request_id": f"o{i}", "profile": "meridale-platform"})
        cb.emit("audit.guardrails", {"request_id": f"p{i}", "profile": "adhd-assistant"})
    ca.emit("audit.tool_calls", {"request_id": "o-x", "decision": "deny"})     # no profile field, same chain
    from audit.chain import chains_of_profile
    mine = chains_of_profile(a.sent + b.sent, "meridale-platform")
    assert len(mine) == 4 and verify_chain(mine, KEY) == []
    del mine[1]
    assert verify_chain(mine, KEY)                                               # a gap is still found


def test_events_read_again_after_a_rebalance_are_not_counted_twice():
    """The dashboard reads Kafka without committing offsets, so after a rebalance (a laptop
    sleeping is enough) it gets every message again. Read twice, an intact chain looks like
    'expected seq 1, found 0'; the buffer must skip what it has already seen."""
    from api.events import EventBuffer
    records = make_chain(4)
    assert verify_chain(records + records, KEY)                  # what the dashboard showed

    buf = EventBuffer()
    for _ in range(3):                                           # read from the start three times
        for offset, r in enumerate(records):
            buf.add("audit.guardrails", r, partition=0, offset=offset)
    assert len(buf.audit_records) == 4 and verify_chain(buf.audit_records, KEY) == []
    assert len(buf.events) == 4
    assert buf.add("audit.guardrails", records[0], partition=1, offset=0)    # another partition is new


def test_events_keep_the_time_they_happened_not_when_the_api_read_them():
    """After a restart the API reads older events again; they must keep their own time, or events
    from different days look like today's (reviewer, 2026-10-28)."""
    from api.events import EventBuffer, message_time

    class Msg:
        def __init__(self, kind, ms):
            self._ts = (kind, ms)

        def timestamp(self):
            return self._ts
    assert message_time(Msg(1, 1_793_000_000_000)) == 1_793_000_000.0      # producer time, in seconds
    assert message_time(Msg(0, -1)) is None                                 # no timestamp available
    buf = EventBuffer()
    buf.add("agent.trace", {"request_id": "r1"}, partition=0, offset=0, at=1_793_000_000.0)
    buf.add("agent.trace", {"request_id": "r2"}, partition=0, offset=1)
    old, new = buf.events
    assert old["at"] == 1_793_000_000.0 and old["received_at"] > old["at"]
    assert new["at"] == new["received_at"]                                  # no Kafka time: when read


def test_every_topic_the_code_writes_audit_events_to_is_read_by_the_verifier():
    """A topic left out of AUDIT_TOPICS is invisible to the verifier: its events look like gaps."""
    import re
    from pathlib import Path

    from audit.chain import AUDIT_TOPICS
    root = Path(__file__).resolve().parent.parent
    written = set()
    for f in root.glob("**/*.py"):
        if ".venv" in f.parts or "tests" in f.parts or not f.is_file():
            continue
        written |= set(re.findall(r'(?:emit|_audit)\(\s*"(audit\.[a-z_]+)"', f.read_text(encoding="utf-8")))
    assert written and written <= set(AUDIT_TOPICS), sorted(written - set(AUDIT_TOPICS))
