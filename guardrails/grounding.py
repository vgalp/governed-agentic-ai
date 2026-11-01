"""Grounding check: an answer may only cite sources the model was actually given.

For profiles that ask the model to cite sources (cite_sources: true), the answer's
source IDs (matched by the profile's source_id_pattern) must all be among the IDs of
the notes passed to the model. An answer that cites anything else is referring to
information it was not given, so it is blocked. Answers with no citations pass.

An invented citation does not always look like a real one. A model asked to cite IDs and
given none wrote "[W1234567890] on [2023-10-01]": neither matches the profile's pattern, so
a check on the pattern alone passes it. Any bracketed token that looks like an identifier
(letters with digits, no spaces) and is not a given ID is therefore also blocked. Masking
placeholders such as [PERSON_1] are not citations and are left alone. Fabricated placeholders
such as [METFORMIN_DOSE] are caught by the profile's output rules, not here.
"""

import re

from guardrails.rules import GuardrailDecision
from profiles.loader import Profile

# [web-1a2b3c4d] [W1234567890] [2023-10-01] [doc 12] but not [PERSON_1] (a masking placeholder),
# not [see below] and not plain prose in brackets.
ID_LIKE = re.compile(r"\[(?![A-Z_]+_\d+\])([A-Za-z0-9][A-Za-z0-9 _.:/-]{2,40})\]")


def looks_like_an_id(token: str) -> bool:
    t = token.strip()
    return any(ch.isdigit() for ch in t) and len(t.split()) <= 2


def check_citations(answer: str, given_ids: list[str], profile: Profile) -> GuardrailDecision:
    if profile.source_id_pattern is None:
        return GuardrailDecision(True)
    given = set(given_ids)
    cited = {m.group(0) for m in profile.source_id_pattern.finditer(answer)}
    unknown = sorted(cited - given)
    invented = sorted({m.group(1) for m in ID_LIKE.finditer(answer)
                       if looks_like_an_id(m.group(1)) and m.group(1) not in given
                       and not profile.source_id_pattern.fullmatch(m.group(1))})
    if unknown or invented:
        what = ", ".join(unknown + [f"[{i}] (not a source ID)" for i in invented])
        return GuardrailDecision(False, "ungrounded",
                                 f"answer cites sources it was not given: {what}",
                                 profile.response("ungrounded"))
    return GuardrailDecision(True)
