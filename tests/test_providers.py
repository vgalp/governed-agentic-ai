"""Hosted model providers (OpenAI, Gemini, Claude), choosing one, and falling back.

No test calls a real service or needs a real key: requests.post is replaced by a fake
that records what would have been sent and returns a recorded-style response."""

import shutil

import pytest
import yaml

from llm import providers
from profiles.loader import ProfileError, load_profile, load_profile_from, select_external_model
from router import router

MSGS = [{"role": "system", "content": "You are a clinic assistant."},
        {"role": "user", "content": "What is the cancellation policy?"}]


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


@pytest.fixture
def fake_post(monkeypatch):
    """Replace requests.post. Set .reply to the response; .sent records the request."""
    class Fake:
        reply = Resp({"choices": [{"message": {"content": "ok"}}]})
        sent = {}

        def __call__(self, url, headers=None, json=None, timeout=None):
            self.sent.update(url=url, headers=headers, json=json)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
    fake = Fake()
    monkeypatch.setattr(providers.requests, "post", fake)
    return fake


def model(provider, **extra):
    return {"provider": provider, "name": f"{provider}-model", "location": "external",
            "api_key_env": "TEST_KEY", **extra}


# --- each provider sends the right request ---------------------------------------

def test_openai(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    assert providers.chat(model("openai"), MSGS) == "ok"
    assert fake_post.sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert fake_post.sent["headers"] == {"Authorization": "Bearer sk-test"}
    assert fake_post.sent["json"]["messages"] == MSGS


def test_gemini_uses_googles_openai_compatible_api(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "g-test")
    assert providers.chat(model("gemini"), MSGS) == "ok"
    assert fake_post.sent["url"] == \
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert fake_post.sent["headers"] == {"Authorization": "Bearer g-test"}


def test_claude_sends_the_system_prompt_separately(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "a-test")
    fake_post.reply = Resp({"type": "message", "content": [{"type": "text", "text": "Patients should "},
                                                           {"type": "text", "text": "cancel 24 hours ahead."}]})
    assert providers.chat(model("anthropic"), MSGS) == "Patients should cancel 24 hours ahead."
    sent = fake_post.sent
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"] == {"x-api-key": "a-test", "anthropic-version": "2023-06-01"}
    assert sent["json"]["system"] == "You are a clinic assistant."
    assert sent["json"]["messages"] == [{"role": "user", "content": "What is the cancellation policy?"}]
    assert sent["json"]["max_tokens"] > 0 and sent["json"]["temperature"] == 0


def test_base_url_can_be_overridden(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "k")
    providers.chat(model("openai", base_url="https://eu.example.com/v1/"), MSGS)
    assert fake_post.sent["url"] == "https://eu.example.com/v1/chat/completions"


def test_the_key_never_goes_in_the_body(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "secret-123")
    for p in ("openai", "gemini", "anthropic"):
        fake_post.reply = Resp({"choices": [{"message": {"content": "ok"}}]} if p != "anthropic"
                               else {"content": [{"type": "text", "text": "ok"}]})
        providers.chat(model(p), MSGS)
        assert "secret-123" not in str(fake_post.sent["json"])


# --- failures become ModelUnavailable, so the router can fall back ----------------

@pytest.mark.parametrize("reply", [
    Resp({}, status=500),                                  # server error
    Resp({}, status=401),                                  # bad key
    Resp(ValueError("not json")),                          # not JSON
    Resp({"choices": []}),                                 # no answer
    providers.requests.ConnectionError("down"),            # cannot connect
])
def test_failures_are_model_unavailable(monkeypatch, fake_post, reply):
    monkeypatch.setenv("TEST_KEY", "k")
    fake_post.reply = reply
    with pytest.raises(providers.ModelUnavailable):
        providers.chat(model("openai"), MSGS)


def test_claude_without_text_is_unavailable(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "k")
    fake_post.reply = Resp({"content": []})
    with pytest.raises(providers.ModelUnavailable):
        providers.chat(model("anthropic"), MSGS)


def test_error_message_never_echoes_the_response(monkeypatch, fake_post):
    monkeypatch.setenv("TEST_KEY", "k")
    fake_post.reply = Resp({"error": "Chester Aufderhar"}, status=400)
    try:
        providers.chat(model("openai"), MSGS)
    except providers.ModelUnavailable as e:
        message = str(e)
    else:
        pytest.fail("expected ModelUnavailable")
    assert "Chester" not in message and "400" in message


def test_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    for p in ("openai", "gemini", "anthropic"):
        assert not providers.is_available(model(p))
        with pytest.raises(providers.ModelUnavailable):
            providers.chat(model(p), MSGS)


# --- profile checks ----------------------------------------------------------------

@pytest.fixture
def clinic_copy(tmp_path):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(load_profile("clinic-assistant").path, dst)

    def edit(change):
        p = dst / "profile.yaml"
        d = yaml.safe_load(p.read_text())
        change(d)
        p.write_text(yaml.safe_dump(d, sort_keys=False))
        return load_profile_from(dst)
    return edit


def test_clinic_lists_three_hosted_providers():
    p = load_profile("clinic-assistant")
    providers_used = {p.models[k]["provider"] for k in router.external_chain(p)}
    assert providers_used == {"openai", "gemini", "anthropic"}


def test_hosted_provider_can_never_be_marked_local(clinic_copy):
    with pytest.raises(ProfileError, match="third-party service"):
        clinic_copy(lambda d: d["models"]["claude"].update(location="local"))


def test_hosted_provider_needs_a_key_variable(clinic_copy):
    with pytest.raises(ProfileError, match="api_key_env"):
        clinic_copy(lambda d: d["models"]["gemini"].pop("api_key_env"))


def test_fallback_must_be_external_models(clinic_copy):
    with pytest.raises(ProfileError, match="local model is always the last resort"):
        clinic_copy(lambda d: d["routing"].update(fallback=["local"]))
    with pytest.raises(ProfileError, match="location external"):
        clinic_copy(lambda d: d["routing"].update(fallback=["no-such-model"]))


def test_fallback_may_not_repeat(clinic_copy):
    with pytest.raises(ProfileError, match="twice"):
        clinic_copy(lambda d: d["routing"].update(fallback=["claude"]))


def test_select_external_model_moves_it_to_the_front():
    p = select_external_model(load_profile("clinic-assistant"), "gemini")
    assert p.routing["external_model"] == "gemini"
    assert "gemini" not in p.routing["fallback"]


def test_select_unknown_external_model_fails():
    with pytest.raises(ProfileError, match="not one of this profile's external models"):
        select_external_model(load_profile("clinic-assistant"), "local")
    with pytest.raises(ProfileError):
        select_external_model(load_profile("adhd-assistant"), "claude")


def test_external_model_setting_applies_to_the_active_profile(monkeypatch):
    monkeypatch.setenv("PROFILE", "clinic-assistant")
    monkeypatch.setenv("EXTERNAL_MODEL", "openai")
    load_profile.cache_clear()
    try:
        assert load_profile().routing["external_model"] == "openai"
        assert load_profile("adhd-assistant").routing["external_model"] is None   # not active: untouched
    finally:
        load_profile.cache_clear()


# --- choosing and falling back -------------------------------------------------------

def keys(monkeypatch, *names):
    for n in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    for n in names:
        monkeypatch.setenv(n, "k")


def policy_external(monkeypatch):
    seen = {}

    def fake(profile, path, input_):
        seen.update(input_)
        return {"target": "external", "reasons": ["no personal information found; external model allowed"]}
    monkeypatch.setattr(router, "decide", fake)
    return seen


def test_configured_model_is_used_when_its_key_is_set(monkeypatch):
    keys(monkeypatch, "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    policy_external(monkeypatch)
    d = router.decide_route(load_profile("clinic-assistant"), [], ["approved_content"])
    assert d.target == "external" and d.model_key == "claude" and d.send_masked


def test_first_model_with_a_key_is_used(monkeypatch):
    keys(monkeypatch, "GEMINI_API_KEY")
    policy_external(monkeypatch)
    d = router.decide_route(load_profile("clinic-assistant"), [], [])
    assert d.model_key == "gemini"
    assert any("claude has no API key set" in r for r in d.reasons)


def test_no_keys_means_local(monkeypatch):
    keys(monkeypatch)
    seen = policy_external(monkeypatch)
    d = router.decide_route(load_profile("clinic-assistant"), [], [])
    assert seen["external_available"] is False and d.target == "local"


def test_patient_data_stays_local_whichever_provider(monkeypatch):
    """Even if the policy said external, record data never leaves (checked in code)."""
    keys(monkeypatch, "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
    policy_external(monkeypatch)
    for choice in ("claude", "openai", "gemini"):
        p = select_external_model(load_profile("clinic-assistant"), choice)
        assert router.decide_route(p, [], ["patient_record"]).target == "local"


def test_fallbacks_try_the_rest_in_order_then_local(monkeypatch):
    keys(monkeypatch, "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
    policy_external(monkeypatch)
    p = load_profile("clinic-assistant")
    first = router.decide_route(p, [], [])
    chain = router.fallbacks(p, first)
    assert [d.model_key for d in chain] == ["openai", "gemini", "local"]
    assert all(d.send_masked for d in chain if d.target == "external")
    assert "claude failed" in chain[0].reasons[0]


def test_fallbacks_skip_models_without_keys(monkeypatch):
    keys(monkeypatch, "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
    policy_external(monkeypatch)
    p = load_profile("clinic-assistant")
    assert [d.model_key for d in router.fallbacks(p, router.decide_route(p, [], []))] == ["gemini", "local"]


def test_no_fallback_after_the_local_model(monkeypatch):
    p = load_profile("clinic-assistant")
    assert router.fallbacks(p, router.local_decision(p)) == []