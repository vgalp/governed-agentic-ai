"""The executor (agents/executor.py) with fakes for Kafka, OPA and the gateway: an approval decided
while it was down is carried out on start, exactly once; a malformed event is skipped; an error
inside a decision is recorded as a failed result instead of stopping the executor."""

import json
from datetime import datetime, timedelta

import pytest

from agents import actions
from agents import executor as ex
from profiles.loader import load_profile


class FakeProducer:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key=None, value=None, **_):
        self.sent.append((topic, json.loads(value)))

    def flush(self, *_):
        return 0

    def poll(self, *_):
        return 0


class FakeAudit:
    def __init__(self):
        self.events, self.producer = [], FakeProducer()

    def emit(self, topic, record):
        self.events.append((topic, record))


@pytest.fixture
def rt(monkeypatch):
    calls = []
    monkeypatch.setattr(ex, "PROFILE", load_profile("clinic-assistant"))
    monkeypatch.setattr(ex, "producer", FakeProducer())
    monkeypatch.setattr(ex, "audit", FakeAudit())
    monkeypatch.setattr(ex, "state", actions.ApprovalState())
    monkeypatch.setattr(ex, "decide", lambda *a, **k: {"allowed": True})

    def call_tool(agent, tool, args, request_id, profile, role=None):
        calls.append(args["approval_id"])
        return {"status": "already_done" if calls.count(args["approval_id"]) > 1 else "done", "summary": "moved"}
    monkeypatch.setattr(ex, "call_tool", call_tool)
    return calls


def requested(approval_id="ap-1"):
    now = datetime.now()
    return {"approval_id": approval_id, "request_id": "r1", "profile": "clinic-assistant",
            "action": "reschedule_appointment", "args": {"appointment_id": "apt-0001", "new_start": "2026-11-09T10:00"},
            "requester": "u1", "requester_role": "front_desk", "approve_roles": ["clinician", "nurse"],
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=30)).isoformat(timespec="seconds")}


def decided(approval_id="ap-1", decision="approve"):
    return {"approval_id": approval_id, "request_id": "r1", "decision": decision, "approver": "u2",
            "approver_role": "nurse", "decided_at": datetime.now().isoformat(timespec="seconds")}


def results():
    return [e for t, e in ex.producer.sent if t == actions.EXECUTED]


def test_an_approval_decided_while_the_executor_was_down_is_carried_out_on_start(rt):
    ex.state.apply(actions.REQUESTED, requested())
    ex.state.apply(actions.DECIDED, decided())                 # read during catch-up: no action yet
    assert ex.finish_interrupted() == 1
    assert rt == ["ap-1"] and results()[0]["status"] == "done"
    ex.state.apply(actions.EXECUTED, results()[0])             # the result comes back around
    assert ex.finish_interrupted() == 0 and rt == ["ap-1"]     # nothing left to finish


def test_finishing_one_that_was_already_made_only_records_the_result(rt):
    rt.append("ap-1")                                          # made before the executor stopped
    ex.state.apply(actions.REQUESTED, requested())
    ex.state.apply(actions.DECIDED, decided())
    ex.finish_interrupted()
    assert results()[0]["status"] == "done"                    # already_done counts as done; one change


def test_rejected_and_pending_approvals_are_not_carried_out_on_start(rt):
    ex.state.apply(actions.REQUESTED, requested("ap-1"))
    ex.state.apply(actions.DECIDED, decided("ap-1", "reject"))
    ex.state.apply(actions.REQUESTED, requested("ap-2"))
    assert ex.finish_interrupted() == 0 and rt == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", json.dumps({"approval_id": "ap-9"}).encode()])
def test_a_malformed_event_is_skipped_in_catch_up_and_live(rt, raw):
    for topic in actions.APPROVAL_TOPICS:
        assert ex.state.apply_raw(topic, raw) in {True, False}  # never raises
        ex.on_event(topic, raw)
    ex.state.apply(actions.REQUESTED, requested())             # the executor still works afterwards
    ex.on_event(actions.DECIDED, json.dumps(decided()).encode())
    assert rt == ["ap-1"]


def test_an_error_inside_a_decision_is_recorded_not_fatal(rt, monkeypatch):
    def broken(*a, **k):
        raise KeyError("action renamed")
    monkeypatch.setattr(ex, "on_decision", broken)
    ex.state.apply(actions.REQUESTED, requested())
    ex.on_event(actions.DECIDED, json.dumps(decided()).encode())
    assert results()[0]["status"] == "failed" and "executor error (KeyError)" in results()[0]["reasons"]


def test_the_executor_does_nothing_at_import():
    """Importing it connects to nothing (main() does): this file could not exist otherwise."""
    assert ex.PROFILE is None or ex.producer is None or isinstance(ex.producer, FakeProducer)
