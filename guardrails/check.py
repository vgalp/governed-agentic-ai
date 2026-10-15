"""One text through the input checks: rules, then the profile's classifier with its narrow
exemptions. Used where there is no planner around the text, for example the arguments an
outside assistant sends to a tool (gateway/endpoint.py).

The result is one of the guard's three labels, with the decision behind it:
  allow     nothing found
  decision  something a person must decide (dose, diagnosis, eligibility, ...)
  crisis    signs that someone may be at risk: always handed to a person, never overridden

Fails closed: if the classifier can not run, the decision blocks (category
guardrail_unavailable), and the label is "decision".
"""

from guardrails.classifier import input_check
from guardrails.rules import GuardrailDecision, check_input, classifier_exemption
from profiles.loader import Profile


def label_of(decision: GuardrailDecision) -> str:
    if decision.allowed:
        return "allow"
    return "crisis" if decision.category == "crisis" else "decision"


def check_text(text: str, profile: Profile) -> tuple[str, GuardrailDecision, str]:
    """(label, decision, layer). A rule block for a non-crisis reason still asks the
    classifier, so a crisis is never reported as something milder."""
    ruled = check_input(text, profile)
    if not ruled.allowed:
        if ruled.category != "crisis":
            second = input_check(text, profile)
            if not second.allowed and second.category == "crisis":
                return "crisis", second, "classifier"
        return label_of(ruled), ruled, "rules"
    flagged = input_check(text, profile)
    if not flagged.allowed and classifier_exemption(text, flagged, profile):
        return "allow", GuardrailDecision(True, None, f"exempt: {flagged.reason}"), "classifier"
    return label_of(flagged), flagged, "classifier"
