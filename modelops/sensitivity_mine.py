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

The same file also gives training text (`label_for_training`, `--purpose train`): sentences from
descriptions the test set did not use. There the cheaper model labels everything and the stronger one
confirms every sentence the first gave a category, and a sample of those it gave none. Training text
needs to be large more than it needs every label checked twice; the sample says how many categories
the first model is missing.

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
    "reproductive_health": r"\babortion|\bmiscarriage|\bcontracepti|\binfertil|\bIVF\b|in vitro fertili[sz]"
                           r"|termination of pregnancy",
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


def choose(rows: list[dict], n_random: int, per_category: int, categories: Iterable[str], seed: int = 23,
           exclude: frozenset[str] = frozenset()) -> list[dict]:
    """Sentences to label: `n_random` from descriptions in random order, then `per_category` from
    descriptions with each category's cue. No description is used twice and no sentence appears twice.
    The same file and seed give the same sentences. `exclude`: ids of descriptions not to touch (the
    ones a test set was taken from)."""
    categories = [c for c in categories if c in CUES]
    rng = random.Random(seed)
    order = [i for i in range(len(rows)) if rows[i][ID_FIELD] not in exclude]
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


Labels = list[tuple[str, ...] | None]        # per sentence: its categories, or None when the reply had no answer for it


def _ask(model: Frontier, sets: sensitivity.ValueSets, items: list[dict], salt: str) -> Labels:
    """One model's label for each sentence, asked in batches."""
    known = set(readable_categories(sets))
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    replies = model.many([(data.label_messages([i["text"] for i in b], sets), 0, f"{salt}:{n}")
                          for n, b in enumerate(batches)], f"labelled by {model.model['name']}")
    out: Labels = []
    for reply, batch in zip(replies, batches):
        got = data.read_labels(reply, len(batch), known)
        out += [got.get(k) for k in range(1, len(batch) + 1)]
    return out


def _row(item: dict, categories: tuple[str, ...], lists: sensitivity.Detector) -> dict:
    """A labelled sentence in the scorer's and the trainer's shape, filed by what the lists catch."""
    if categories:
        caught = {f.category for f in lists.of_text(item["text"])}
        stratum = "explicit" if set(categories) <= caught else "implicit"
    else:
        stratum = "hard_negative" if item["picked"] == "enriched" else "plain"
    return {"id": f"{item['source_id']}#{item['sentence']}", "kind": "text", "text": item["text"],
            "categories": list(categories), "stratum": stratum, "picked": item["picked"],
            "cue_for": item["cue_for"], "has_cue": item["has_cue"]}


def label(first: Frontier, second: Frontier, sets: sensitivity.ValueSets, items: list[dict], salt: str,
          seed: int = 23) -> tuple[list[dict], list[dict], dict]:
    """Both models label every sentence, in mixed batches so a batch says nothing about where its
    sentences came from. Returns the rows both agree on (ready for the scorer), the rest for a person, and
    a report."""
    lists = sensitivity.Detector(sets)
    mixed = items[:]
    random.Random(seed).shuffle(mixed)
    answers = [_ask(model, sets, mixed, salt) for model in (first, second)]
    agreed, disputed = [], []
    for item, a, b in zip(mixed, *answers):
        if a is None or a != b:
            disputed.append({**item, first.model["name"]: _shown(a), second.model["name"]: _shown(b)})
        else:
            agreed.append(_row(item, a, lists))
    agreed.sort(key=lambda r: r["id"])
    report = {"sentences": len(items), "agreed": len(agreed), "not_agreed": len(disputed),
              "labellers": [first.model["name"], second.model["name"]],
              "by_stratum": dict(Counter(r["stratum"] for r in agreed)),
              "by_category": dict(Counter(c for r in agreed for c in r["categories"])),
              "with_a_category": {"from_random": sum(bool(r["categories"]) for r in agreed if r["picked"] == "random"),
                                  "random": sum(r["picked"] == "random" for r in agreed)}}
    return agreed, disputed, report


def label_for_training(cheap: Frontier, strong: Frontier, sets: sensitivity.ValueSets, items: list[dict], salt: str,
                       check_share: float = 0.1, seed: int = 29) -> tuple[list[dict], dict]:
    """Training rows: `cheap` labels every sentence; `strong` labels again every sentence `cheap` gave a
    category and `check_share` of those it gave none. A sentence labelled twice is kept only when both
    agree. A sentence labelled once (no category, not in the sample) is kept on the first label alone;
    the report says how often the sample contradicted such a label."""
    lists = sensitivity.Detector(sets)
    rng = random.Random(seed)
    mixed = items[:]
    rng.shuffle(mixed)
    first = _ask(cheap, sets, mixed, f"{salt}:first")
    with_category = [i for i, a in enumerate(first) if a]
    without = [i for i, a in enumerate(first) if a == ()]
    sample = rng.sample(without, round(check_share * len(without)))
    checked = sorted(with_category + sample)
    second = dict(zip(checked, _ask(strong, sets, [mixed[i] for i in checked], f"{salt}:second")))
    rows, rejected = [], Counter()
    for i, (item, a) in enumerate(zip(mixed, first)):
        if a is None:
            rejected["no label from the first model"] += 1
        elif i in second and second[i] != a:
            rejected["the second model disagreed, on a sentence the first gave a category" if a
                     else "the second model found a category in a sampled sentence"] += 1
        else:
            rows.append({**_row(item, a, lists), "labelled_twice": i in second})
    rows.sort(key=lambda r: r["id"])
    missed = rejected["the second model found a category in a sampled sentence"]
    report = {"sentences": len(items), "kept": len(rows), "left_out": dict(rejected),
              "labellers": {"all": cheap.model["name"], "confirming": strong.model["name"]},
              "labelled_twice": len(checked), "sample_of_no_category": len(sample),
              "sample_where_second_found_a_category": missed,
              "by_stratum": dict(Counter(r["stratum"] for r in rows)),
              "by_category": dict(Counter(c for r in rows for c in r["categories"]))}
    return rows, report


def split_by_description(rows: list[dict], dev_share: float, seed: int = 7) -> tuple[list[dict], list[dict]]:
    """Train and validation rows with no description in both, so a validation sentence never sits beside
    its neighbours in training."""
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["id"].rsplit("#", 1)[0], []).append(r)
    sources = sorted(by_source)
    random.Random(seed).shuffle(sources)
    dev: list[dict] = []
    while sources and len(dev) < dev_share * len(rows):
        dev += by_source[sources.pop()]
    return [r for s in sorted(sources) for r in by_source[s]], dev


def used_descriptions(manifest_file: Path) -> frozenset[str]:
    """The descriptions a test set's sentences came from, read from its manifest."""
    rows = json.loads(manifest_file.read_text(encoding="utf-8"))["rows"]
    return frozenset(r["id"].rsplit("#", 1)[0] for r in rows)


def estimate_training_usd(items: list[dict], cheap: dict, strong: dict, twice_share: float = 0.3) -> float:
    """A rough cost of `label_for_training`: everything once with the cheap model, and about `twice_share`
    of it (the sentences with a category, and the sample) again with the strong one."""
    return round(estimate_usd(items, [cheap]) + twice_share * estimate_usd(items, [strong]), 2)


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
