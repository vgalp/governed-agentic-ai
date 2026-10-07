"""Mask personal information before it reaches any AI model, and restore it afterward.

Which entities are masked, and which words are never masked, come from the
active profile (privacy section of profiles/<name>/profile.yaml).
"""

from presidio_analyzer import AnalyzerEngine

from profiles.loader import Profile, load_profile

# Loading the analyzer takes a few seconds, so it is created once.
analyzer = AnalyzerEngine()


def mask(text: str, profile: Profile | None = None) -> tuple[str, dict[str, str]]:
    """Replace personal details with tokens such as [PERSON_1].

    Returns the masked text and a private mapping of token -> original value.
    The mapping never leaves this service.
    """
    profile = profile or load_profile()
    results = analyzer.analyze(
        text=text,
        entities=list(profile.privacy_entities),
        language="en",
        allow_list=list(profile.privacy_allow_list),
    )

    # Remove overlapping detections, keeping the longest one at each position.
    results = sorted(results, key=lambda r: (r.start, -(r.end - r.start)))
    kept = []
    last_end = -1
    for r in results:
        if r.start >= last_end:
            kept.append(r)
            last_end = r.end

    mapping: dict[str, str] = {}   # token -> original
    reverse: dict[str, str] = {}   # original -> token (same value gets the same token)
    counters: dict[str, int] = {}
    masked = text

    # Replace from the end of the text backwards so positions stay valid.
    for r in sorted(kept, key=lambda r: r.start, reverse=True):
        original = text[r.start:r.end]
        if original in reverse:
            token = reverse[original]
        else:
            counters[r.entity_type] = counters.get(r.entity_type, 0) + 1
            token = f"[{r.entity_type}_{counters[r.entity_type]}]"
            mapping[token] = original
            reverse[original] = token
        masked = masked[:r.start] + token + masked[r.end:]

    return masked, mapping


def unmask(text: str, mapping: dict[str, str]) -> str:
    """Put the original values back in place of their tokens."""
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text