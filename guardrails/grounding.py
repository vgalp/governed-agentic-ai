"""Grounding check: an answer may only cite sources the model was actually given.

For profiles that ask the model to cite sources (cite_sources: true), the answer's
source IDs (matched by the profile's source_id_pattern) must all be among the IDs of
the notes passed to the model. An answer that cites anything else is referring to
information it was not given, so it is blocked. Answers with no citations pass.
Fabricated placeholders such as [METFORMIN_DOSE] are caught by the profile's output
rules, not here.
"""

from guardrails.rules import GuardrailDecision
from profiles.loader import Profile


def check_citations(answer: str, given_ids: list[str], profile: Profile) -> GuardrailDecision:
    if profile.source_id_pattern is None:
        return GuardrailDecision(True)
    cited = {m.group(0) for m in profile.source_id_pattern.finditer(answer)}
    unknown = sorted(cited - set(given_ids))
    if unknown:
        return GuardrailDecision(False, "ungrounded",
                                 f"answer cites sources it was not given: {', '.join(unknown)}",
                                 profile.response("ungrounded"))
    return GuardrailDecision(True)