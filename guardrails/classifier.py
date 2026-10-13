"""Layer 2 guardrails: a safety classifier (Llama Guard 3 8B via Ollama) with
the active profile's own safety policy (profiles/<name>/classifier_policy.md).

The QUESTION can instead be checked by the fine-tuned guard classifier (ModernBERT, models/<name>),
when the profile sets classifier input_engine: guard_model. On frozen test set v3 it missed 1 of 30
crisis messages, against 26 for rules then Llama Guard (eval/results/guard_test_v3_notes.md).
Answers are always checked by Llama Guard: the guard classifier is trained on questions only.

Layer 1 rules catch known words. This layer catches meaning the rules miss,
such as indirect crisis language ("Everyone would be better off without me").

Why a custom policy: on the dev set, Llama Guard's built-in policy caught 1/5
crisis prompts; the same model with this policy caught 4/5, with no false flags
(eval/results/guard_compare_dev_notes.md).

It runs locally, so the original (unmasked) text never leaves the system.
It fails CLOSED: if the classifier can't be reached, the request is blocked.
"""

from dataclasses import dataclass, field

import requests

from guardrails.rules import GuardrailDecision
from profiles.loader import Profile, load_profile

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"

# Llama Guard 3 prompt format. {role} is "User" (check the question) or
# "Agent" (check the model's answer in the context of the question).
TEMPLATE = """<|begin_of_text|><|start_header_id|>user<|end_header_id|>

Task: Check if there is unsafe content in '{role}' messages in conversations according our safety policy with the below categories.

<BEGIN UNSAFE CONTENT CATEGORIES>
{policy}
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>

{conversation}

<END CONVERSATION>

Provide your safety assessment for ONLY THE LAST {role} message in the above conversation:
 - First line must read 'safe' or 'unsafe'.
 - If unsafe, a second line must include a comma-separated list of violated categories.<|eot_id|><|start_header_id|>assistant<|end_header_id|>

"""


@dataclass
class GuardOutput:
    safe: bool
    codes: list[str] = field(default_factory=list)
    raw: str = ""


def parse_guard_output(raw: str) -> GuardOutput:
    """Parse Llama Guard output: 'safe', or 'unsafe' followed by a line like 'S1,S2'."""
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if lines and lines[0].lower() == "safe":
        return GuardOutput(True, [], raw)
    codes = []
    if len(lines) > 1:
        codes = [c.strip().upper() for c in lines[1].split(",") if c.strip()]
    # Anything other than a clear "safe" is treated as unsafe.
    return GuardOutput(False, codes, raw)


def build_prompt(user_text: str, assistant_text: str | None = None,
                 profile: Profile | None = None) -> str:
    """Build the classifier prompt for a question, or for an answer to a question."""
    profile = profile or load_profile()
    conversation = f"User: {user_text}"
    role = "User"
    if assistant_text is not None:
        conversation += f"\n\nAgent: {assistant_text}"
        role = "Agent"
    return TEMPLATE.format(role=role, policy=profile.classifier.policy, conversation=conversation)


def classify(user_text: str, assistant_text: str | None = None, model: str | None = None,
             profile: Profile | None = None) -> GuardOutput:
    profile = profile or load_profile()
    resp = requests.post(OLLAMA_GENERATE_URL, json={
        "model": model or profile.classifier.model,
        "prompt": build_prompt(user_text, assistant_text, profile),
        "raw": True,                      # we supply the full Llama Guard template
        "stream": False,
        "options": {"temperature": 0},
    }, timeout=120)
    resp.raise_for_status()
    return parse_guard_output(resp.json()["response"])


def to_decision(result: GuardOutput, profile: Profile | None = None) -> GuardrailDecision:
    """Map policy codes to guardrail decisions. The profile lists categories in
    priority order, so the first code found wins (crisis wins over everything)."""
    if result.safe:
        return GuardrailDecision(True)
    profile = profile or load_profile()
    cats = profile.classifier.categories
    names = {c.code: c.name for c in cats}
    reason = "classifier: " + (", ".join(
        f"{c} {names.get(c, 'unknown')}" for c in result.codes) or "unsafe")
    for c in cats:
        if c.code in result.codes:
            return GuardrailDecision(False, c.category, reason, profile.response(c.category))
    # An unknown code, or no code: refuse with the default category (fail safe).
    category = profile.classifier.default_category
    return GuardrailDecision(False, category, reason, profile.response(category))


def classifier_check(user_text: str, assistant_text: str | None = None,
                     profile: Profile | None = None) -> GuardrailDecision:
    """Layer 2 check. Fails closed if the classifier is unavailable."""
    profile = profile or load_profile()
    try:
        return to_decision(classify(user_text, assistant_text, profile=profile), profile)
    except (requests.RequestException, KeyError, ValueError) as e:
        return GuardrailDecision(False, "guardrail_unavailable", f"classifier error: {e}",
                                 profile.response("unavailable"))


_guards: dict = {}


def load_guard(profile: Profile):
    """The profile's guard classifier, loaded once per process (it needs the train group: torch)."""
    name = profile.classifier.guard_model
    if name not in _guards:
        from classifier.model import GuardModel
        from classifier.train import MODELS
        _guards[name] = GuardModel(MODELS / name)
    return _guards[name]


def guard_check(user_text: str, profile: Profile) -> GuardrailDecision:
    """The guard classifier on a question: allow, decision (the profile's decision_category) or crisis.
    Fails closed, like Llama Guard: if the model cannot run, the question is blocked."""
    try:
        label, probs = load_guard(profile).label(profile.classifier.guard_policy, user_text)
    except Exception as e:  # noqa: BLE001 - any failure to label must block, never allow
        return GuardrailDecision(False, "guardrail_unavailable", f"guard classifier error: {e}",
                                 profile.response("unavailable"))
    reason = (f"guard classifier {profile.classifier.guard_model}: {label} "
              f"(crisis {probs['crisis']:.2f}, decision {probs['decision']:.2f})")
    if label == "allow":
        return GuardrailDecision(True, None, reason)
    category = "crisis" if label == "crisis" else profile.classifier.decision_category
    return GuardrailDecision(False, category, reason, profile.response(category))


def input_check(user_text: str, profile: Profile | None = None) -> GuardrailDecision:
    """Layer 2 on a question, with the engine the profile chose."""
    profile = profile or load_profile()
    if profile.classifier.input_engine == "guard_model":
        return guard_check(user_text, profile)
    return classifier_check(user_text, profile=profile)


def input_engine_name(profile: Profile) -> str:
    """What checks questions, for logs and the trace."""
    c = profile.classifier
    return c.guard_model if c.input_engine == "guard_model" else c.model


def crisis_over_rule(rule_check: GuardrailDecision, user_text: str,
                     profile: Profile | None = None) -> GuardrailDecision | None:
    """A rule blocked the question for a reason other than crisis (say, a medication word).
    Ask the classifier too: if it sees a crisis, the crisis decision wins, so the person gets the
    crisis reply. A rule may make the guard stricter, never less safe. None: keep the rule's block."""
    if rule_check.allowed or rule_check.category == "crisis":
        return None
    second = input_check(user_text, profile)
    return second if not second.allowed and second.category == "crisis" else None