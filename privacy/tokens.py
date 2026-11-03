"""Masking tokens, without Presidio: put values back for the right reader.

`privacy.masking.mask` makes the tokens ([PERSON_1], [LOCATION_2]) and the private mapping; these
helpers restore them. They are pure string functions, kept apart so the pipeline and its tests can
import them without loading the analyzer (which takes seconds and a spaCy model).
"""


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
