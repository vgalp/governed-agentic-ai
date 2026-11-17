"""Masking tokens, without Presidio: put values back for the right reader.

`privacy.masking.mask` makes the tokens ([PERSON_1], [LOCATION_2]) and the private mapping; these
helpers restore them. They are pure string functions, kept apart so the pipeline and its tests can
import them without loading the analyzer (which takes seconds and a spaCy model).
"""

import re

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATES = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"                                  # 1990-09-15
    r"|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"                       # 9/15/1990, 15.09.90
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}}\b"      # 15 Sep 1990
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",     # September 15, 1990
    re.IGNORECASE)
_LONG_NUMBERS = re.compile(r"\b\d[\d -]{4,}\d\b")                # record numbers, phone and card fragments: 6+ digits


def scrub_for_audit(value):
    """The same structure with every date and long number in its text replaced by [DATE] and [NUMBER].

    The last step before text is written to the audit trail, after masking. Masking for a model must
    keep dates (a visit date is clinical content), so a date of birth typed into a question survives
    it; an audit record needs neither. Tokens such as [PERSON_1] are left as they are."""
    if isinstance(value, str):
        return _LONG_NUMBERS.sub(lambda m: m.group(0) if sum(c.isdigit() for c in m.group(0)) < 6 else "[NUMBER]",
                                 _DATES.sub("[DATE]", value))
    if isinstance(value, dict):
        return {k: scrub_for_audit(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_for_audit(v) for v in value]
    return value


def entity_type(token: str) -> str:
    """[LOCATION_2] -> LOCATION."""
    return token.strip("[]").rsplit("_", 1)[0]


def restore_types(text: str, mapping: dict[str, str], types: tuple[str, ...]) -> str:
    """The masked text with only the tokens of these entity types put back (e.g. LOCATION), so a
    tool or the local model can see a place name without ever seeing a person's name or contact
    details."""
    for token, original in mapping.items():
        if entity_type(token) in types:
            text = text.replace(token, original)
    return text


def unmask(text: str, mapping: dict[str, str]) -> str:
    """Put the original values back in place of their tokens."""
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text
