"""Model routing: the code-side rules hold whatever the policy returns.
The policy itself is tested with OPA: opa test policies/ -v"""

import shutil

import pytest
import requests
import yaml

from llm import providers
from profiles.loader import load_profile, load_profile_from
from router import router


@pytest.fixture
def external_profile(tmp_path, monkeypatch):
    """A copy of the ADHD profile that may send masked text to an external model."""
    dst = tmp_path / "adhd-assistant"
    shutil.copytree(load_profile("adhd-assistant").path, dst)
    p = dst / "profile.yaml"
    d = yaml.safe_load(p.read_text())
    d["models"]["external"] = {"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
                               "name": "big-model", "location": "external", "api_key_env": "TEST_EXT_KEY"}
    d["routing"].update(external_model="external", external_allowed=True, send_external_when="masked",
                        local_input="original")
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    monkeypatch.setenv("TEST_EXT_KEY", "test-key")
    return load_profile_from(dst)


def policy_says(monkeypatch, result=None, error=None):
    seen = {}

    def fake_decide(profile, path, input_):
        seen.update(path=path, input=input_)
        if error:
            raise error
        return result
    monkeypatch.setattr(router, "decide", fake_decide)
    return seen


def test_entity_types_are_kinds_not_values():
    mapping = {"[PERSON_1]": "John Smith", "[PHONE_NUMBER_1]": "416-555-0199", "[PERSON_2]": "Ann"}
    assert router.entity_types(mapping) == ["PERSON", "PHONE_NUMBER"]


def test_policy_input_holds_facts_only(monkeypatch, external_profile):
    seen = policy_says(monkeypatch, {"target": "local", "reasons": ["x"]})
    router.decide_route(external_profile, ["PERSON"], ["approved_content"])
    assert seen["path"] == "routing/decision"
    assert seen["input"] == {"profile": "adhd-assistant", "entity_types": ["PERSON"],
                             "context_labels": ["approved_content"], "external_available": True}


def test_local_decision(monkeypatch, external_profile):
    policy_says(monkeypatch, {"target": "local", "reasons": ["contains personal information"]})
    d = router.decide_route(external_profile, ["PERSON"], [])
    assert d.target == "local" and d.model["name"] == "mistral"
    assert d.send_masked is False                     # this profile lets the local model see the original


def test_external_decision_is_always_masked(monkeypatch, external_profile):
    policy_says(monkeypatch, {"target": "external", "reasons": ["ok"]})
    d = router.decide_route(external_profile, ["PERSON"], [])
    assert d.target == "external" and d.model["name"] == "big-model"
    assert d.send_masked is True                      # even though local_input is "original"


def test_closed_profile_never_goes_external_even_if_policy_says_so(monkeypatch):
    policy_says(monkeypatch, {"target": "external", "reasons": ["ok"]})
    d = router.decide_route(load_profile("adhd-assistant"), [], [])
    assert d.target == "local"
    assert any("overridden" in r for r in d.reasons)


def test_missing_api_key_means_local(monkeypatch, external_profile):
    monkeypatch.delenv("TEST_EXT_KEY")
    seen = policy_says(monkeypatch, {"target": "external", "reasons": ["ok"]})
    d = router.decide_route(external_profile, [], [])
    assert seen["input"]["external_available"] is False
    assert d.target == "local"


def test_unreachable_policy_fails_safe_to_local(monkeypatch, external_profile):
    policy_says(monkeypatch, error=requests.ConnectionError("down"))
    d = router.decide_route(external_profile, [], [])
    assert d.target == "local" and "fail safe" in d.reasons[0]


def test_unexpected_policy_result_is_local(monkeypatch, external_profile):
    policy_says(monkeypatch, None)
    assert router.decide_route(external_profile, [], []).target == "local"


def test_routing_audit_never_holds_personal_values(monkeypatch, external_profile):
    policy_says(monkeypatch, {"target": "external", "reasons": ["ok"]})
    events = []

    class FakeChain:
        def emit(self, topic, event, flush=False):
            events.append((topic, event))
    monkeypatch.setattr(router, "get_audit_chain", lambda name: FakeChain())
    router.route(external_profile, {"[PERSON_1]": "John Smith"}, ["search_knowledge"], "r1", "planner")
    topic, event = events[0]
    assert topic == "audit.routing"
    assert event["target"] == "external" and event["sent_masked"] is True
    assert event["entity_types"] == ["PERSON"] and event["context_labels"] == ["approved_content"]
    assert "John" not in str(event) and "test-key" not in str(event)


# --- providers ------------------------------------------------------------------

def test_mock_provider_is_deterministic():
    m = {"provider": "mock", "name": "mock", "location": "local"}
    msgs = [{"role": "user", "content": "hello"}]
    assert providers.chat(m, msgs) == providers.chat(m, msgs)


def test_external_provider_needs_its_key(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    m = {"provider": "openai_compatible", "base_url": "https://x/v1", "name": "m",
         "location": "external", "api_key_env": "NO_SUCH_KEY"}
    assert not providers.is_available(m)
    with pytest.raises(providers.ModelUnavailable):
        providers.chat(m, [{"role": "user", "content": "hi"}])


def test_external_provider_sends_key_only_in_header(monkeypatch):
    monkeypatch.setenv("K", "secret-123")
    sent = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json)
        return Resp()
    monkeypatch.setattr(providers.requests, "post", fake_post)
    m = {"provider": "openai_compatible", "base_url": "https://x/v1/", "name": "m",
         "location": "external", "api_key_env": "K"}
    assert providers.chat(m, [{"role": "user", "content": "hi"}]) == "ok"
    assert sent["url"] == "https://x/v1/chat/completions"
    assert sent["headers"] == {"Authorization": "Bearer secret-123"}
    assert "secret-123" not in str(sent["json"])
