"""A test set for the sensitive-category readers from text people wrote (ADR 038 step 3).

Every other set here was written by a language model or by the author of the lists. This one takes
sentences from published patient descriptions (a JSON-lines file with an id and a text per row, such as
NCBI Open-Patients: case reports from PubMed Central, exam vignettes and TREC topics) and has two
frontier models label each sentence without seeing the other's answer. A sentence both label the same
way is kept; the rest are written to a file for a person and left out.

Which sentences are labelled:

    random     sentences from descriptions chosen at random: what ordinary clinical prose looks like, so
               the false-mark rate on it means something.
    enriched   sentences from descriptions that mention a category somewhere (a broad cue word, written
               for this file and not taken from the lists), a share for each category: where the
               positives are. From each description, some sentences with the cue and some without.

The detectors being measured take no part in choosing sentences, so a sentence they would miss is as
likely to be chosen as one they would catch.

How a kept sentence is filed, so the scorer's table can be read as before:

    explicit        it has a category and the word lists catch every one
    implicit        it has a category the lists miss (implied, or named in words the lists do not hold)
    hard_negative   it has none and comes from a description with a cue
    plain           it has none and comes from a description chosen at random

The text is other people's, under their licence. It is written beside the source file, outside this
repository; what is recorded in the run is a manifest (source ids, labels and a hash of each sentence),
enough to rebuild the set from the same source file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from classifier import clean as text_clean
from modelops import sensitivity_data as data
from modelops.frontier import Frontier
from modelops.sanitize import pii_in
from privacy import sensitivity
from privacy.sensitivity_model import readable_categories

ID_FIELD, TEXT_FIELD = "_id", "description"
MIN_WORDS, MAX_WORDS = 6, 60
BATCH = 15
PER_DESCRIPTION = 2                  # sentences taken of each kind from one description
OUTPUT_TOKENS_PER_TEXT = 40          # measured on the first run's labelling calls
PROMPT_TOKENS = 450                  # the labeller's rules

# Broad words that say a description touches a category somewhere. Deliberately not the lists: a
# description found by "miscarriage" holds sentences that say it in other words.
CUES: dict[str, re.Pattern] = {k: re.compile(p, re.I) for k, p in {
    "substance_use": r"\balcohol|\bdrinking\b|\bheroin|\bcocaine|\bopioid|\bmethadone|drug use|\boverdos|\bintoxicat|\bcannabis",
    "mental_health": r"\bdepress|\bschizophren|\bbipolar|\bpsychiatr|\bsuicid|\banxiety\b|\bpsychosis|\bpsychotic",
    "hiv": r"\bHIV\b|\bAIDS\b|\bantiretroviral|\bCD4\b",
    "sexual_health": r"\bsyphilis|\bgonorrh|\bchlamydia|sexually transmitted|\bgenital (?:herpes|warts)|\bSTIs?\b|\bSTDs?\b",
    # Pregnancy in general is not in this category (privacy/sensitivity.yaml), so it is not a cue.
    "reproductive_health": r"\babortion|\bmiscarriage|\bcontracepti|\binfertil|\bIVF\b|in vitro fertili[sz]|termination of pregnancy",
    "violence": r"\bassault|\babuse[ds]?\b|domestic violence|\braped?\b|\bbattered|intimate partner|non-accidental",
    "genetic": r"\bmutation|genetic test|\bBRCA|\bcarrier\b|\bheterozygous|\bhomozygous|\bhereditary|\bkaryotyp",
}.items()}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


class SourceError(ValueError):
    """The source file is not rows of {id, text}."""


def sentences(text: str) -> list[str]:
    """The description's sentences of a usable length. A rough split: a wrong cut makes an odd sentence,
    which the labellers still judge as written."""
    parts = (" ".join(p.split()) for p in _SENTENCE_END.split(text or ""))
    return [p for p in parts if MIN_WORDS <= len(p.split()) <= MAX_WORDS]


def read_source(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise SourceError(f"{path.name} line {n} is not JSON") from None
            if not isinstance(row, dict) or not isinstance(row.get(ID_FIELD), str) or not isinstance(row.get(TEXT_FIELD), str):
                raise SourceError(f"{path.name} line {n} needs a '{ID_FIELD}' and a '{TEXT_FIELD}'")
            rows.append(row)
    if not rows:
        raise SourceError(f"{path.name} has no rows")
    return rows


def choose(rows: list[dict], n_random: int, per_category: int, categories: Iterable[str], seed: int = 23) -> list[dict]:
    """Sentences to label: `n_random` from descriptions in random order, then `per_category` from
    descriptions with each category's cue. No description is used twice and no sentence appears twice.
    The same file and seed give the same sentences."""
    categories = [c for c in categories if c in CUES]
    rng = random.Random(seed)
    order = list(range(len(rows)))
    rng.shuffle(order)
    chosen: list[dict] = []
    seen: set[str] = set()
    need = Counter(dict.fromkeys(categories, per_category))
    taken_random = 0

    def take(row: dict, index: int, text: str, picked: str, cue_for: str, has_cue: bool) -> bool:
        key = text_clean.norm(text)
        if key in seen or pii_in(text):
            return False
        seen.add(key)
        chosen.append({"source_id": row[ID_FIELD], "sentence": index, "text": text, "picked": picked,
                       "cue_for": cue_for, "has_cue": has_cue})
        return True

    for i in order:
        row = rows[i]
        found = list(enumerate(sentences(row[TEXT_FIELD])))
        if not found:
            continue
        if taken_random < n_random:
            for index, text in rng.sample(found, min(PER_DESCRIPTION, len(found))):
                taken_random += take(row, index, text, "random", "", False)
            continue
        wanted = [c for c in categories if need[c] > 0 and CUES[c].search(row[TEXT_FIELD])]
        if not wanted:
            if not +need:
                break
            continue
        category = max(wanted, key=lambda c: need[c])                 # the category furthest from its share
        with_cue = [(n, t) for n, t in found if CUES[category].search(t)]
        without = [(n, t) for n, t in found if not CUES[category].search(t)]
        for group, has_cue in ((with_cue, True), (without, False)):
            for index, text in rng.sample(group, min(PER_DESCRIPTION, len(group))):
                need[category] -= take(row, index, text, "enriched", category, has_cue)
    return chosen


def estimate_usd(items: list[dict], models: list[dict]) -> float:
    """A rough cost of labelling `items` once with each model, from the prices in loop.yaml."""
    batches = -(-len(items) // BATCH)
    words = sum(len(i["text"].split()) for i in items)
    tokens_in, tokens_out = words * 1.4 + batches * PROMPT_TOKENS, len(items) * OUTPUT_TOKENS_PER_TEXT
    return round(sum((tokens_in * m["price"]["input_per_million"] + tokens_out * m["price"]["output_per_million"]) / 1e6
                     for m in models), 2)


def label(first: Frontier, second: Frontier, sets: sensitivity.ValueSets, items: list[dict], salt: str,
          seed: int = 23) -> tuple[list[dict], list[dict], dict]:
    """Both models label every sentence, in mixed batches so a batch says nothing about where its
    sentences came from. Returns the rows both agree on (ready for the scorer), the rest for a person, and
    a report."""
    known = set(readable_categories(sets))
    lists = sensitivity.Detector(sets)
    mixed = items[:]
    random.Random(seed).shuffle(mixed)
    batches = [mixed[i:i + BATCH] for i in range(0, len(mixed), BATCH)]
    answers = []
    for model in (first, second):
        replies = model.many([(data.label_messages([i["text"] for i in b], sets), 0, f"{salt}:{n}")
                              for n, b in enumerate(batches)], f"labelled by {model.model['name']}")
        answers.append([data.read_labels(r, len(b), known) for r, b in zip(replies, batches)])
    agreed, disputed = [], []
    for n, batch in enumerate(batches):
        for k, item in enumerate(batch, 1):
            a, b = answers[0][n].get(k), answers[1][n].get(k)
            if a is None or a != b:
                disputed.append({**item, first.model["name"]: _shown(a), second.model["name"]: _shown(b)})
                continue
            caught = {f.category for f in lists.of_text(item["text"])}
            if a:
                stratum = "explicit" if set(a) <= caught else "implicit"
            else:
                stratum = "hard_negative" if item["picked"] == "enriched" else "plain"
            agreed.append({"id": f"{item['source_id']}#{item['sentence']}", "kind": "text", "text": item["text"],
                           "categories": list(a), "stratum": stratum, "picked": item["picked"],
                           "cue_for": item["cue_for"], "has_cue": item["has_cue"]})
    agreed.sort(key=lambda r: r["id"])
    report = {"sentences": len(items), "agreed": len(agreed), "not_agreed": len(disputed),
              "labellers": [first.model["name"], second.model["name"]],
              "by_stratum": dict(Counter(r["stratum"] for r in agreed)),
              "by_category": dict(Counter(c for r in agreed for c in r["categories"])),
              "with_a_category": {"from_random": sum(bool(r["categories"]) for r in agreed if r["picked"] == "random"),
                                  "random": sum(r["picked"] == "random" for r in agreed)}}
    return agreed, disputed, report


def _shown(categories: tuple[str, ...] | None) -> str:
    return "unreadable" if categories is None else ", ".join(categories) or "none"


def manifest(rows: list[dict], source: Path, report: dict) -> dict:
    """What is kept in the repository: enough to rebuild the set from the same source file, and none of its text."""
    return {"source_file": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "note": "Sentences from the source file, labelled blind by two models and kept where they agree. The text "
                    "is not stored here: it belongs to the source's authors, under the source's licence.",
            "report": report,
            "rows": [{"id": r["id"], "categories": r["categories"], "stratum": r["stratum"], "picked": r["picked"],
                      "text_sha256": hashlib.sha256(r["text"].encode()).hexdigest()} for r in rows]}


def write_disputed(path: Path, rows: list[dict]) -> None:
    """The sentences the labellers did not agree on, with both answers and an empty column for a person."""
    if not rows:
        return
    fields = [*rows[0].keys(), "your_label"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({**r, "your_label": ""} for r in rows)
