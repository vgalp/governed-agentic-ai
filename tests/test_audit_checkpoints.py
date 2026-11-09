"""Signed audit checkpoints (ADR 030): deleting the newest audit events, rewriting one, or deleting a
whole chain is found, which the hash chain alone cannot do; a changed checkpoint file is found too."""

import json

import pytest

from audit import chain as ch
from audit.chain import AuditChain, Checkpoints, read_checkpoints, verify_chain, verify_checkpoints

KEY = b"test-key"


class Bus:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key=None, value=None):
        self.sent.append(json.loads(value))

    def flush(self, *a):
        return 0


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "CHECKPOINT_SECONDS", 3600)      # no timer threads in tests
    now = [1000.0]
    bus = Bus()
    c = AuditChain(bus, "planner", KEY, Checkpoints(tmp_path, KEY, clock=lambda: now[0]))
    return c, bus, tmp_path, now


def emit(c, n, profile="clinic-assistant"):
    for i in range(n):
        c.emit("audit.tool_calls", {"request_id": f"r{i}", "profile": profile, "tool": "search"})


def check(bus, tmp_path, now, **kw):
    return verify_checkpoints(bus.sent, read_checkpoints(tmp_path), KEY, now=now[0] + 60, **kw)


def test_an_intact_chain_matches_its_checkpoints(setup):
    c, bus, tmp_path, now = setup
    emit(c, ch.CHECKPOINT_EVERY + 5)                          # one checkpoint on the way
    assert c.checkpoint()["seq"] == ch.CHECKPOINT_EVERY + 4   # and one for the rest (as at exit)
    problems, summary = check(bus, tmp_path, now)
    assert problems == [] and summary["chains_checked"] == 1
    assert verify_chain(bus.sent, KEY) == []


def test_deleting_the_newest_events_is_found(setup):
    c, bus, tmp_path, now = setup
    emit(c, 10)
    c.checkpoint()
    truncated = bus.sent[:7]
    assert verify_chain(truncated, KEY) == []                 # the chain alone cannot see it
    problems, _ = verify_checkpoints(truncated, read_checkpoints(tmp_path), KEY, now=now[0] + 60)
    assert problems and "ends at seq 6" in problems[0]


def test_rewriting_the_checkpointed_event_is_found(setup):
    c, bus, tmp_path, now = setup
    emit(c, 3)
    c.checkpoint()
    bus.sent[-1]["audit"]["hash"] = "f" * 64
    problems, _ = check(bus, tmp_path, now)
    assert any("differs from its checkpoint" in p for p in problems)


def test_deleting_a_whole_chain_is_found(setup):
    c, bus, tmp_path, now = setup
    emit(c, 4)
    c.checkpoint()
    problems, _ = verify_checkpoints([], read_checkpoints(tmp_path), KEY, now=now[0] + 60)
    assert any("all 4 checkpointed events are missing" in p for p in problems)


def test_a_changed_checkpoint_file_is_found(setup):
    c, bus, tmp_path, now = setup
    emit(c, 3)
    c.checkpoint()
    emit(c, 2)
    c.checkpoint()
    f = next(tmp_path.glob("*.jsonl"))
    lines = [json.loads(x) for x in f.read_text().splitlines()]
    lines[-1]["seq"] = 2                                      # pretend the newest events never existed
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    problems, _ = check(bus, tmp_path, now)
    assert any("checkpoint file was changed" in p for p in problems)
    f.write_text("\n".join(json.dumps(x) for x in lines[:1]) + "\n")   # dropping the last line is not caught
    assert check(bus, tmp_path, now)[0] == []                 # by the file itself: that is why it lives off-box


def test_an_unkeyed_checkpoint_is_refused_when_a_key_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "CHECKPOINT_SECONDS", 3600)
    bus = Bus()
    c = AuditChain(bus, "planner", KEY, Checkpoints(tmp_path, None, clock=lambda: 0.0))
    emit(c, 2)
    c.checkpoint()
    problems, _ = verify_checkpoints(bus.sent, read_checkpoints(tmp_path), KEY, now=100.0)
    assert any("checkpoint file was changed" in p for p in problems)


def test_a_checkpoint_newer_than_the_settle_time_is_not_judged_yet(setup):
    c, bus, tmp_path, now = setup
    emit(c, 5)
    c.checkpoint()
    problems, summary = verify_checkpoints(bus.sent[:2], read_checkpoints(tmp_path), KEY, now=now[0] + 5)
    assert problems == [] and summary["chains_checked"] == 0     # its events may still be arriving


def test_only_chains_of_the_profile_are_judged(setup):
    c, bus, tmp_path, now = setup
    emit(c, 3, profile="county-benefits")
    c.checkpoint()
    problems, summary = verify_checkpoints([], read_checkpoints(tmp_path), KEY, profile="clinic-assistant",
                                           now=now[0] + 60)
    assert problems == [] and summary["chains_checked"] == 0


def test_nothing_new_writes_no_checkpoint_and_chains_from_before_are_counted(setup):
    c, bus, tmp_path, now = setup
    assert c.checkpoint() is None                             # no events yet
    emit(c, 1)
    c.checkpoint()
    assert c.checkpoint() is None                             # nothing new since
    old = {"audit": {"chain_id": "planner-old", "seq": 0, "hash": "x"}}
    _, summary = verify_checkpoints([*bus.sent, old], read_checkpoints(tmp_path), KEY, now=now[0] + 60)
    assert summary["chains_without_checkpoint"] == 1


def test_every_fiftieth_event_writes_a_checkpoint(setup):
    c, bus, tmp_path, now = setup
    emit(c, ch.CHECKPOINT_EVERY * 2)
    lines = read_checkpoints(tmp_path)[c.chain_id]
    assert [x["seq"] for x in lines] == [ch.CHECKPOINT_EVERY - 1, ch.CHECKPOINT_EVERY * 2 - 1]
