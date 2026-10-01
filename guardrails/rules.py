"""Layer 1 guardrails: deterministic rules applied before and after the model.

Fixed responses are DRAFTS pending clinician approval.
"""

import re
from dataclasses import dataclass

# --- Patterns ---------------------------------------------------------------

MEDICATION_TERMS = [
    "adderall", "ritalin", "vyvanse", "concerta", "focalin", "dexedrine",
    "methylphenidate", "amphetamines?", "dextroamphetamine", "lisdexamfetamine",
    "strattera", "atomoxetine", "qelbree", "viloxazine", "intuniv", "guanfacine",
    "clonidine", "wellbutrin", "bupropion", "stimulants?", "medications?", "meds",
    "prescriptions?", "pills?",
]
MEDICATION_RE = re.compile(r"\b(" + "|".join(MEDICATION_TERMS) + r")\b", re.IGNORECASE)

# Words that turn a medication mention into a request for medical advice.
DOSE_INTENT_RE = re.compile(
    r"\b(how much|how many|dose|doses|dosage|dosing|increase|decrease|reduce|"
    r"raise|lower|stop|stopping|skip|double|switch|change|side effects?|"
    r"mix|combine|alcohol|safe to take)\b",
    re.IGNORECASE,
)
DOSE_AMOUNT_RE = re.compile(r"\b\d+(\.\d+)?\s?(mg|milligrams?|mcg)\b", re.IGNORECASE)

DIAGNOSIS_RE = re.compile(
    r"\b(do i have|could i have|diagnose|diagnosis|am i adhd|is it adhd|test me for)\b",
    re.IGNORECASE,
)

CRISIS_RE = re.compile(
    r"\b(suicid\w*|kill myself|end my life|end it all|want to die|"
    r"hurt(ing)? myself|self[- ]?harm\w*|no reason to live|overdose)\b",
    re.IGNORECASE,
)

# --- Fixed responses (DRAFT: pending clinician approval) ---------------------

CRISIS_RESPONSE = (
    "It sounds like you're going through something really hard, and you deserve "
    "support right now. In the U.S., call or text 988 (Suicide & Crisis Lifeline). "
    "In Canada, call or text 9-8-8. If you are in immediate danger, call 911. "
    "I'm not able to help with this, but trained people are available right now."
)
MEDICATION_RESPONSE = (
    "I can't give advice about medication, including doses, timing, or changes. "
    "Please ask the clinician who prescribes it, or a pharmacist. I'm happy to help "
    "with planning, routines, or a list of questions for your next appointment."
)
DIAGNOSIS_RESPONSE = (
    "I can't diagnose ADHD or any other condition. A doctor or licensed clinician "
    "can assess this properly. If it helps, I can help you write down what you've "
    "noticed so you can bring it to an appointment."
)


@dataclass
class GuardrailDecision:
    allowed: bool
    category: str | None = None   # "crisis", "medication", "diagnosis"
    reason: str | None = None
    response: str | None = None   # fixed response to send instead


def check_input(text: str) -> GuardrailDecision:
    """Decide whether a user message may go to the model at all."""
    if CRISIS_RE.search(text):
        return GuardrailDecision(False, "crisis", "crisis language in input", CRISIS_RESPONSE)
    if MEDICATION_RE.search(text) and (DOSE_INTENT_RE.search(text) or DOSE_AMOUNT_RE.search(text)):
        return GuardrailDecision(False, "medication", "medication advice requested", MEDICATION_RESPONSE)
    if DIAGNOSIS_RE.search(text):
        return GuardrailDecision(False, "diagnosis", "diagnosis requested", DIAGNOSIS_RESPONSE)
    return GuardrailDecision(True)


def check_output(text: str) -> GuardrailDecision:
    """Decide whether a model answer may be shown to the user."""
    if MEDICATION_RE.search(text) or DOSE_AMOUNT_RE.search(text):
        return GuardrailDecision(False, "medication", "model answer mentions medication", MEDICATION_RESPONSE)
    if CRISIS_RE.search(text):
        return GuardrailDecision(False, "crisis", "model answer contains crisis language", CRISIS_RESPONSE)
    return GuardrailDecision(True)