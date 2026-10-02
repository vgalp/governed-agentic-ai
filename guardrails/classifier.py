"""Layer 2 guardrails: a safety classifier (Llama Guard 3 8B via Ollama) with
this project's own safety policy.

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

from guardrails.rules import CRISIS_RESPONSE, GuardrailDecision

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
GUARD_MODEL = "llama-guard3:8b"

# Our safety policy, written in Llama Guard's category format.
POLICY = """S1: Suicide and Self-Harm Risk.
Messages that express suicidal thoughts or intent, a wish to die or to not exist, not wanting
to wake up, hopelessness about continuing to live, feeling that others would be better off
without them, saying goodbye or giving away possessions, or asking about ways to harm
themselves. Includes indirect or euphemistic wording.
S2: Medical Advice.
Requests for, or advice about, medication dosing, timing, combining, sharing, starting,
stopping or switching medication, or a diagnosis of a medical or mental health condition."""

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

# Our categories -> this deployment's guardrail categories.
CATEGORY_NAMES = {"S1": "suicide and self-harm risk", "S2": "medical advice"}

# Fixed responses (DRAFT: pending clinician approval).
MEDICAL_ADVICE_RESPONSE = (
    "I can't give medical advice. A doctor, pharmacist, or other licensed clinician "
    "is the right person for this. I'm happy to help you prepare questions for them."
)
UNAVAILABLE_RESPONSE = (
    "Sorry, I can't answer right now because a safety check is unavailable. "
    "Please try again shortly. If you are in crisis, call or text 988 in the U.S. "
    "or 9-8-8 in Canada, or call 911 in an emergency."
)


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


def build_prompt(user_text: str, assistant_text: str | None = None) -> str:
    """Build the classifier prompt for a question, or for an answer to a question."""
    conversation = f"User: {user_text}"
    role = "User"
    if assistant_text is not None:
        conversation += f"\n\nAgent: {assistant_text}"
        role = "Agent"
    return TEMPLATE.format(role=role, policy=POLICY, conversation=conversation)


def classify(user_text: str, assistant_text: str | None = None, model: str = GUARD_MODEL) -> GuardOutput:
    resp = requests.post(OLLAMA_GENERATE_URL, json={
        "model": model,
        "prompt": build_prompt(user_text, assistant_text),
        "raw": True,                      # we supply the full Llama Guard template
        "stream": False,
        "options": {"temperature": 0},
    }, timeout=120)
    resp.raise_for_status()
    return parse_guard_output(resp.json()["response"])


def to_decision(result: GuardOutput) -> GuardrailDecision:
    """Map policy categories to guardrail decisions. Crisis wins over everything."""
    if result.safe:
        return GuardrailDecision(True)
    reason = "classifier: " + (", ".join(
        f"{c} {CATEGORY_NAMES.get(c, 'unknown')}" for c in result.codes) or "unsafe")
    if "S1" in result.codes:
        return GuardrailDecision(False, "crisis", reason, CRISIS_RESPONSE)
    # S2, an unknown code, or no code: refuse with a referral (fail safe).
    return GuardrailDecision(False, "medical_advice", reason, MEDICAL_ADVICE_RESPONSE)


def classifier_check(user_text: str, assistant_text: str | None = None) -> GuardrailDecision:
    """Layer 2 check. Fails closed if the classifier is unavailable."""
    try:
        return to_decision(classify(user_text, assistant_text))
    except (requests.RequestException, KeyError, ValueError) as e:
        return GuardrailDecision(False, "guardrail_unavailable", f"classifier error: {e}", UNAVAILABLE_RESPONSE)