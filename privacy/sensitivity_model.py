"""A language model as the reader of text for sensitive categories (ADR 038, step 3).

The lists (privacy/sensitivity.yaml) know words. They cannot know that "finishes a fifth of vodka most
nights" is about alcohol. A language model can. This module makes one usable as the detector's text
model:

    from privacy import sensitivity, sensitivity_model
    sets = sensitivity.load()
    reader = sensitivity_model.local_reader("qwen2.5:7b", sets)      # a model served by Ollama
    sensitivity.Detector(sets, model=reader).of_text("He finishes a fifth of vodka most nights.")

It is for measuring first (eval/run_sensitivity.py --model). One call takes about a second on a
laptop, so it suits a question (one call) and not a chart (a call per record). What it finds on the
development set decides whether a small classifier is trained from it for records.

A local model only: the text it reads is the text the category rules exist to keep in the building.

The model answers yes or no per category, so every category it names is returned with probability 1.
A reply that cannot be read counts as "nothing found by the model" and is counted (`unreadable`): the
lists still apply, and the caller can see how often it happened.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping

from privacy.sensitivity import ValueSets

Ask = Callable[[list[dict]], str]           # chat messages -> the model's reply

SYSTEM = """You read one piece of text from a health clinic: a staff member's message, or a line from a \
patient's record. Say which protected categories of health information it reveals about a particular person.

Categories:
{categories}

Rules:
- Name a category when the text states it or clearly implies it, even if it never uses the category's words.
- Name it only when the text is about a particular person: a patient, the writer, or someone they mention. \
A question about policy, a general fact, a guideline or equipment is about no one: name nothing.
- A finding that is denied or ruled out ("no history of ...", "denies ...") is not named.
- A word used in another sense (a heart tracing, a figure of speech, another illness) is not named.
- Most text reveals none of these.

Reply with JSON only, no other words: {{"categories": ["<category name>", ...]}}
If none applies: {{"categories": []}}"""


def readable_categories(sets: ValueSets) -> dict[str, str]:
    """The categories a reader of text can find, with what counts for each. A category the lists know only
    from a record system's label (nothing to read for) is left out."""
    return {c.key: c.description for c in sets.categories.values() if c.icd10 or c.drugs or c.phrases}


def messages_for(text: str, sets: ValueSets) -> list[dict]:
    listed = "\n".join(f"- {key}: {what}" for key, what in readable_categories(sets).items())
    return [{"role": "system", "content": SYSTEM.format(categories=listed)},
            {"role": "user", "content": f"Text:\n{text}"}]


def parse(reply: str, known: set[str]) -> list[str] | None:
    """The categories in the reply, or None when it holds no readable answer. Names the reply invented
    are dropped; a model sometimes adds one.

    Besides {"categories": [...]}, two shorter forms are read, because a local model uses them for
    "none": an empty object, and a bare list of names (usually empty)."""
    cleaned = re.sub(r"```(?:json)?", "", reply or "").strip()
    match = re.search(r"\{.*\}|\[.*\]", cleaned, re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if obj == {}:
        return []
    named = obj.get("categories") if isinstance(obj, dict) else obj
    if not isinstance(named, list):
        return None
    return [c for c in dict.fromkeys(n for n in named if isinstance(n, str)) if c in known]


class LlmReader:
    """text -> {category: 1.0}, by asking a language model. Counts its calls and unreadable replies."""

    def __init__(self, ask: Ask, sets: ValueSets, clock: Callable[[], float] = time.monotonic):
        self._ask, self._sets, self._clock = ask, sets, clock
        self._known = set(readable_categories(sets))
        self.calls = self.unreadable = 0
        self.seconds: list[float] = []

    def __call__(self, text: str) -> Mapping[str, float]:
        started = self._clock()
        reply = self._ask(messages_for(text, self._sets))
        self.seconds.append(self._clock() - started)
        self.calls += 1
        named = parse(reply, self._known)
        if named is None:
            self.unreadable += 1
            return {}
        return dict.fromkeys(named, 1.0)


def local_reader(name: str, sets: ValueSets) -> LlmReader:
    """A reader backed by a model served by Ollama on this machine, at temperature 0."""
    from llm import providers

    model = {"provider": "ollama", "name": name, "location": "local", "temperature": 0}
    return LlmReader(lambda messages: providers.complete(model, messages)[0], sets)
