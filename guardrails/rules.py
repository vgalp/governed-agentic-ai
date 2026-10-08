"""Layer 1 guardrails: deterministic rules applied before and after the model.

The rules themselves live in the active profile (profiles/<name>/rules.yaml);
this module only evaluates them. Fixed replies come from the profile's
responses.yaml (DRAFTS pending clinician approval).
"""

import re
from dataclasses import dataclass

from profiles.loader import Profile, Rule, load_profile

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class GuardrailDecision:
    allowed: bool
    category: str | None = None   # e.g. "crisis", "medication", "diagnosis"
    reason: str | None = None
    response: str | None = None   # fixed response to send instead


def rule_matches(rule: Rule, text: str) -> bool:
    """All 'all' patterns and at least one 'any' pattern (if given) must match, and no
    'none' pattern may match, in the whole text or, for scope 'sentence', within a
    single sentence. 'none' is always checked against the whole text."""
    if any(p.search(text) for p in rule.none):
        return False
    segments = [text] if rule.scope == "text" else SENTENCE_SPLIT_RE.split(text)
    return any(
        all(p.search(s) for p in rule.all) and (not rule.any or any(p.search(s) for p in rule.any))
        for s in segments
    )


def classifier_exemption(text: str, decision: GuardrailDecision, profile: Profile | None = None) -> str | None:
    """If an INPUT classifier flag falls under a narrow exemption in the profile (for
    example a pure record lookup flagged as medical advice), return the exemption's
    reason. Crisis flags are never exempt (the loader refuses such rules)."""
    profile = profile or load_profile()
    if decision.allowed or decision.category == "crisis":
        return None
    for rule in profile.classifier_exemptions:
        if rule.category == decision.category and rule_matches(rule, text):
            return rule.reason
    return None


def _check(rules: tuple[Rule, ...], text: str, profile: Profile) -> GuardrailDecision:
    for rule in rules:
        if rule_matches(rule, text):
            return GuardrailDecision(False, rule.category, rule.reason, profile.response(rule.category))
    return GuardrailDecision(True)


def check_input(text: str, profile: Profile | None = None) -> GuardrailDecision:
    """Decide whether a user message may go to the model at all."""
    profile = profile or load_profile()
    return _check(profile.input_rules, text, profile)


def check_output(text: str, profile: Profile | None = None) -> GuardrailDecision:
    """Decide whether a model answer may be shown to the user."""
    profile = profile or load_profile()
    return _check(profile.output_rules, text, profile)