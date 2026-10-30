"""The guard classifier in the pipeline: profile settings, labels to replies, failing closed, and a
crisis winning over a rule's block. A fake model stands in, so no torch is needed."""

from dataclasses import replace

import pytest

from guardrails import classifier as gc
from guardrails.rules import GuardrailDecision, check_input
from profiles.loader import ProfileError, load_profile, load_profile_from


class FakeGuard:
    def __init__(self, label, crisis=0.0, decision=0.0):
        self.result = (label, {"allow": 1 - crisis - decision, "decision": decision, "crisis": crisis})
        self.seen = []

    def label(self, policy, text):
        self.seen.append((policy, text))
        return self.result


@pytest.fixture
def adhd(monkeypatch):
    p = load_profile("adhd-assistant")
    monkeypatch.setattr(gc, "_guards", {})
    return p


def use(monkeypatch, guard):
    monkeypatch.setattr(gc, "load_guard", lambda profile: guard)


def test_the_training_profiles_check_questions_with_the_guard_classifier():
    from classifier.generate import POLICIES
    for name, decision in (("adhd-assistant", "medical_advice"), ("clinic-assistant", "medical_advice"),
                           ("county-benefits", "eligibility_decision")):
        c = load_profile(name).classifier
        assert (c.input_engine, c.guard_model, c.decision_category) == ("guard_model", "guard-modernbert-v6.1", decision)
        assert c.guard_policy == POLICIES[name]                  # the paragraph it was trained with
        assert c.model == "llama-guard3:8b"                       # answers still go to Llama Guard


def test_labels_become_the_profile_replies(adhd, monkeypatch):
    use(monkeypatch, FakeGuard("crisis", crisis=0.91))
    d = gc.input_check("I've started giving my things away.", adhd)
    assert (d.allowed, d.category, d.response) == (False, "crisis", adhd.response("crisis"))
    assert "crisis 0.91" in d.reason
    use(monkeypatch, FakeGuard("decision", decision=0.8))
    d = gc.input_check("Can I skip my meds on Saturday?", adhd)
    assert (d.allowed, d.category) == (False, "medical_advice")
    use(monkeypatch, FakeGuard("allow"))
    assert gc.input_check("Help me plan tomorrow.", adhd).allowed


def test_the_guard_reads_the_profile_policy_with_the_question(adhd, monkeypatch):
    guard = FakeGuard("allow")
    use(monkeypatch, guard)
    gc.input_check("Help me plan tomorrow.", adhd)
    assert guard.seen == [(adhd.classifier.guard_policy, "Help me plan tomorrow.")]


def test_a_guard_that_cannot_run_blocks_the_question(adhd, monkeypatch):
    def broken(profile):
        raise RuntimeError("no model")
    monkeypatch.setattr(gc, "load_guard", broken)
    d = gc.input_check("Help me plan tomorrow.", adhd)
    assert (d.allowed, d.category, d.response) == (False, "guardrail_unavailable", adhd.response("unavailable"))


def test_llamaguard_profiles_are_unchanged(adhd, monkeypatch):
    p = replace(adhd, classifier=replace(adhd.classifier, input_engine="llamaguard"))
    monkeypatch.setattr(gc, "classifier_check", lambda text, profile=None: GuardrailDecision(True, None, "llama"))
    assert gc.input_check("anything", p).reason == "llama"
    assert gc.input_engine_name(p) == "llama-guard3:8b" and gc.input_engine_name(adhd) == "guard-modernbert-v6.1"


def test_a_crisis_wins_over_a_rule_block(adhd, monkeypatch):
    text = "I've been looking up how many of my pills it would take."
    ruled = check_input(text, adhd)
    assert not ruled.allowed and ruled.category != "crisis"      # a medication rule catches it first
    use(monkeypatch, FakeGuard("crisis", crisis=0.95))
    won = gc.crisis_over_rule(ruled, text, adhd)
    assert won.category == "crisis" and won.response == adhd.response("crisis")
    use(monkeypatch, FakeGuard("decision", decision=0.9))
    assert gc.crisis_over_rule(ruled, text, adhd) is None        # not a crisis: the rule's block stands


def test_crisis_over_rule_leaves_crisis_and_allowed_alone(adhd, monkeypatch):
    def never(*a, **k):
        raise AssertionError("the classifier should not be asked")
    monkeypatch.setattr(gc, "input_check", never)
    assert gc.crisis_over_rule(GuardrailDecision(True), "hi", adhd) is None
    assert gc.crisis_over_rule(GuardrailDecision(False, "crisis"), "I want to die", adhd) is None


def write_profile(tmp_path, classifier_extra: str):
    src = load_profile("adhd-assistant").path
    dst = tmp_path / "adhd-assistant"
    import shutil
    shutil.copytree(src, dst)
    y = (dst / "profile.yaml").read_text()
    start = y.index("  input_engine:")
    end = y.index("\n\n", start)
    (dst / "profile.yaml").write_text(y[:start] + classifier_extra + y[end:])
    return dst


def test_bad_guard_settings_are_refused(tmp_path):
    with pytest.raises(ProfileError, match="input_engine must be one of"):
        load_profile_from(write_profile(tmp_path / "a", "  input_engine: bert"))
    with pytest.raises(ProfileError, match="guard_model"):
        load_profile_from(write_profile(tmp_path / "b", "  input_engine: guard_model\n  decision_category: medical_advice"))
    with pytest.raises(ProfileError, match="no response for not_a_reply"):
        load_profile_from(write_profile(tmp_path / "c", "  input_engine: guard_model\n  guard_model: g\n"
                                                         "  decision_category: not_a_reply"))