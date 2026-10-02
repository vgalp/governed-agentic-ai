"""Unit tests for layer 2 parsing, prompts and mapping. They don't call the model."""

import requests

from guardrails import classifier
from guardrails.classifier import build_prompt, parse_guard_output, to_decision


def test_safe_output():
    r = parse_guard_output("safe")
    assert r.safe and r.codes == []
    assert to_decision(r).allowed


def test_self_harm_maps_to_crisis_with_988():
    d = to_decision(parse_guard_output("unsafe\nS1"))
    assert not d.allowed and d.category == "crisis"
    assert "988" in d.response


def test_medical_advice_maps_to_referral():
    d = to_decision(parse_guard_output("unsafe\nS2"))
    assert not d.allowed and d.category == "medical_advice"


def test_crisis_wins_when_several_codes():
    d = to_decision(parse_guard_output("unsafe\nS2,S1"))
    assert d.category == "crisis"


def test_unexpected_output_is_treated_as_unsafe():
    r = parse_guard_output("I'm not sure")
    assert not r.safe
    assert not to_decision(r).allowed


def test_input_prompt_checks_user_message():
    p = build_prompt("Help me plan tomorrow.")
    assert "ONLY THE LAST User message" in p
    assert "Agent:" not in p


def test_output_prompt_checks_agent_answer_in_context():
    p = build_prompt("Help me plan tomorrow.", "1. Wake up at 7.")
    assert "ONLY THE LAST Agent message" in p
    assert "User: Help me plan tomorrow." in p and "Agent: 1. Wake up at 7." in p


def test_fails_closed_when_classifier_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(classifier.requests, "post", boom)
    d = classifier.classifier_check("Help me plan tomorrow.")
    assert not d.allowed and d.category == "guardrail_unavailable"