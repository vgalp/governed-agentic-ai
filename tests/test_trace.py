"""Decision trace: events are well formed, ordered, timed, and hold no personal data."""

import json

import pytest

from agents.trace import TRACE_TOPIC, Trace, ids_text, masking_summary, tool_agent
from api.events import EVENT_TOPICS


class FakeProducer:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key=None, value=None):
        self.sent.append((topic, key, json.loads(value)))

    def poll(self, timeout):
        return 0


@pytest.fixture
def trace():
    return Trace(FakeProducer(), "clinic-assistant", "req-1", role="nurse")


def test_step_is_published_to_the_trace_topic(trace):
    trace.step("intake", "input_rules", "pass", "No input rule matched")
    topic, key, event = trace.producer.sent[0]
    assert topic == TRACE_TOPIC == "agent.trace"
    assert key == "req-1"
    assert event["agent"] == "intake" and event["step"] == "input_rules" and event["outcome"] == "pass"
    assert event["profile"] == "clinic-assistant" and event["role"] == "nurse" and event["process"] == "planner"


def test_steps_are_numbered_in_order(trace):
    for step in ("received", "input_rules", "masking"):
        trace.step("intake", step, "info", step)
    assert [e["seq"] for _, _, e in trace.producer.sent] == [0, 1, 2]


def test_a_started_step_gets_a_duration(trace):
    trace.start("answer", "model", "Asking mistral")
    trace.step("answer", "model", "info", "Draft answer ready")
    running, done = (e for _, _, e in trace.producer.sent)
    assert running["outcome"] == "running" and running["duration_ms"] is None
    assert done["duration_ms"] is not None and done["duration_ms"] >= 0


def test_a_step_without_start_has_no_duration(trace):
    assert trace.step("router", "route", "info", "Local model")["duration_ms"] is None


def test_unknown_agent_or_outcome_is_refused(trace):
    with pytest.raises(ValueError):
        trace.step("someone", "x", "pass", "x")
    with pytest.raises(ValueError):
        trace.step("intake", "x", "maybe", "x")


def test_masking_summary_gives_kinds_and_counts_never_values():
    mapping = {"[PERSON_1]": "Chester Aufderhar", "[PHONE_NUMBER_1]": "416-555-0199", "[PERSON_2]": "Sarah Lee"}
    summary = masking_summary(mapping)
    assert summary == "Masked 3 personal detail(s): 2 PERSON, 1 PHONE_NUMBER"
    for value in mapping.values():
        assert value not in summary


def test_masking_summary_when_nothing_was_masked():
    assert masking_summary({}) == "No personal details found"


def test_tools_map_to_agents():
    assert tool_agent("search_records") == "records"
    assert tool_agent("search_knowledge") == "knowledge"


def test_long_id_lists_are_shortened():
    assert ids_text(["a", "b"]) == "a, b"
    assert ids_text([f"med-{i}" for i in range(8)], limit=3) == "med-0, med-1, med-2 and 5 more"


def test_the_dashboard_follows_the_trace_topic():
    assert TRACE_TOPIC in EVENT_TOPICS