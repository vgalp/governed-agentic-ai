"""Training and test text for the sensitive-category classifier, written and labelled by frontier models
(ADR 038 step 3, on the machinery of ADR 037).

The word lists know named and coded information. This data teaches a small model the rest: text that
implies a category without naming it, and text that looks like a category and is not.

How a row is made:

    plan      one cell per (category, kind of text, angle): explicit wording, implied wording, look-alikes
              that are not the category, two categories at once, and everyday text.
    write     one model writes a batch of short texts for a cell, told what the category covers.
    label     ANOTHER model labels the same texts without seeing what they were written for.
    keep      only texts where the writer's intent and the blind label are the same set of categories.
    sort      a text written as "implied" that the word lists already catch is filed as explicit, so
              "implied" means what it says: the lists find nothing in it.
    clean     duplicates, text with something that looks like real contact details, and anything close to
              the hand-written development set are dropped.

Training text is written by the loop's frontier model and labelled by its reviewer model. The test set is
the other way round: written by the reviewer, labelled by the frontier model. So no model is tested on
its own writing. Both are still language-model writing: a score on the test set says more than a score on
the training split, and less than a score on text people wrote.

Nothing here trains a model or decides what it is used for (modelops/sensitivity_train.py,
eval/run_sensitivity.py).
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from classifier import clean as text_clean
from modelops.frontier import Frontier, parse_json
from modelops.sanitize import pii_in
from privacy import sensitivity
from privacy.sensitivity_model import SYSTEM as READER_RULES
from privacy.sensitivity_model import readable_categories

BATCH = 10
MIN_WORDS, MAX_WORDS = 3, 80
TOO_CLOSE = 0.75
STRATA = ("explicit", "implicit", "hard_negative", "plain")

# What a batch is asked for. An angle is one way of being that kind of text; each is its own cell so
# no one way dominates the set.
ANGLES: dict[str, tuple[str, ...]] = {
    "explicit": (
        "names the condition or its treatment in ordinary clinical words",
        "a line from a problem list, a medication list or a visit record that names it",
        "a staff member asking the assistant about a named patient, using the usual words for it",
    ),
    "implicit": (
        "describes what the person does or what happened, never naming the condition",
        "is about the logistics of care (where they go, how often, which programme or clinic), never naming the condition",
        "reports what a relative, partner or colleague said or noticed, never naming the condition",
        "uses the shorthand, slang or abbreviations staff or patients really use, never the textbook words",
        "is the person's own words to a staff member, indirect or embarrassed, never naming the condition",
    ),
    "hard_negative": (
        "uses one of the category's usual words in another sense (a test result, a device, a figure of speech)",
        "says the finding is absent, denied or ruled out",
        "asks about policy, guidelines, forms or supplies, about no particular person",
        "is about another condition or an ordinary event that could be mistaken for it at a glance",
    ),
}
PLAIN_ANGLES = (
    "scheduling, reminders and phone calls",
    "ordinary long-term conditions such as blood pressure, diabetes, asthma and arthritis",
    "vaccines, vital signs, allergies and routine check-ups",
    "injuries, infections and surgery with no protected category",
    "ordinary medications, refills and pharmacy calls",
    "laboratory and imaging results with no protected category",
    "billing, insurance and forms",
    "referrals, transport, interpreters and equipment",
    "general questions to the assistant about opening hours, rooms and supplies",
    "children's visits, school forms and growth checks",
)

# Identical requests give similar text. Each batch of a cell is set somewhere else.
SETTINGS = ("a primary care clinic", "a hospital ward", "a front desk or call centre", "a community health centre",
            "an emergency department", "a school nurse's office", "a county benefits office", "a pharmacy or billing office")

WRITER_SYSTEM = ("You write short synthetic text for training a privacy classifier used in health clinics. Everything "
                 "is invented: never use a real person's details. Reply with JSON only.")


@dataclass(frozen=True)
class Cell:
    """One request to a writer: `categories` the texts should fall in (none for negatives), how, and which batch."""
    stratum: str
    categories: tuple[str, ...]
    angle: str
    about: str = ""          # for a look-alike: the category it resembles
    batch: int = 0

    def key(self) -> str:
        return f"{self.stratum}:{'+'.join(self.categories) or 'none'}:{self.about}:{self.angle}:{self.batch}"


def plan(sets: sensitivity.ValueSets, batches_per_cell: int, seed: int = 7) -> list[Cell]:
    """Every cell, `batches_per_cell` times: per readable category, each explicit, implied and look-alike
    angle; pairs of categories that occur together; and everyday text. Deterministic for a seed."""
    cats = list(readable_categories(sets))
    rng = random.Random(seed)
    pairs = [tuple(sorted(rng.sample(cats, 2))) for _ in range(len(cats))]
    cells: list[Cell] = []
    for b in range(batches_per_cell):
        for c in cats:
            cells += [Cell("explicit", (c,), a, batch=b) for a in ANGLES["explicit"]]
            cells += [Cell("implicit", (c,), a, batch=b) for a in ANGLES["implicit"]]
            cells += [Cell("hard_negative", (), a, about=c, batch=b) for a in ANGLES["hard_negative"]]
        cells += [Cell("implicit", p, ANGLES["implicit"][i % len(ANGLES["implicit"])], batch=b)
                  for i, p in enumerate(dict.fromkeys(pairs))]
        cells += [Cell("plain", (), a, batch=b) for a in PLAIN_ANGLES]
    return cells


def write_messages(cell: Cell, sets: sensitivity.ValueSets) -> list[dict]:
    what = readable_categories(sets)
    forms = ("Make about half of them messages a staff member types to an AI assistant, and half lines from a "
             f"patient's record or a note. Set most of them in {SETTINGS[cell.batch % len(SETTINGS)]}. Vary length "
             "from 6 to 40 words and give people varied invented names. Some may have abbreviations or a typo, as "
             "real text does.")
    if cell.stratum == "plain":
        ask = (f"Write {BATCH} short texts from a health clinic about {cell.angle}. None of them may reveal anything "
               f"about substance use, mental health, HIV, sexually transmitted infections, reproductive health, "
               f"abuse or violence, or genetics.")
    elif cell.stratum == "hard_negative":
        ask = (f"The category is: {what[cell.about]}\n\nWrite {BATCH} short texts that could be mistaken for this "
               f"category and are NOT in it, nor in any other protected category. Each one {cell.angle}. A careful "
               f"reader should agree that the text reveals nothing protected about any particular person.")
    else:
        described = "\n".join(f"- {what[c]}" for c in cell.categories)
        both = " Each text must reveal all of them about the same person." if len(cell.categories) > 1 else ""
        ask = (f"The categor{'ies are' if len(cell.categories) > 1 else 'y is'}:\n{described}\n\nWrite {BATCH} short "
               f"texts that reveal this about a particular person.{both} Each one {cell.angle}. A clinician reading it "
               f"should recognise it without doubt.")
        if cell.stratum == "implicit":
            ask += (" Do not use the name of the condition, the names of its medications, or its usual clinical or "
                    "legal terms anywhere in the text.")
    return [{"role": "system", "content": WRITER_SYSTEM},
            {"role": "user", "content": f"{ask}\n\n{forms}\n\nReply as JSON: {{\"texts\": [\"...\", ...]}} with exactly "
                                        f"{BATCH} texts."}]


def read_texts(reply: str) -> list[str]:
    """The texts in a writer's reply; none when it cannot be read (the batch is skipped and counted)."""
    obj = parse_json(reply)
    texts = obj.get("texts") if obj else None
    if not isinstance(texts, list):
        return []
    return [" ".join(t.split()) for t in texts if isinstance(t, str) and t.strip()]


def label_messages(texts: list[str], sets: sensitivity.ValueSets) -> list[dict]:
    """The reader's own rules (privacy/sensitivity_model.py), for a numbered batch. The labeller never sees what
    the texts were written for."""
    listed = "\n".join(f"- {key}: {what}" for key, what in readable_categories(sets).items())
    rules = READER_RULES.format(categories=listed).split("Reply with JSON only")[0].rstrip()
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
    return [{"role": "system", "content": rules.replace("You read one piece of text", "You read numbered pieces of text")
             + "\n\nJudge each numbered text on its own."},
            {"role": "user", "content": f"{numbered}\n\nReply with JSON only: {{\"labels\": [{{\"n\": 1, \"categories\": "
                                        f"[\"<category name>\", ...]}}, ...]}} with one entry for each of the "
                                        f"{len(texts)} texts, and an empty list where none applies."}]


def read_labels(reply: str, n: int, known: set[str]) -> dict[int, tuple[str, ...]]:
    """{text number: its categories, sorted}. A text the labeller skipped, or labelled with a name that is not
    a category, is left out: it will not be kept."""
    obj = parse_json(reply)
    items = obj.get("labels") if obj else None
    out: dict[int, tuple[str, ...]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("categories"), list):
            continue
        try:
            k = int(str(item.get("n", "")).strip().rstrip("."))
        except ValueError:
            continue
        named = item["categories"]
        if 1 <= k <= n and all(isinstance(c, str) and c in known for c in named):
            out[k] = tuple(sorted(set(named)))
    return out


def held_out_texts(dev_set: Path) -> list[str]:
    """The hand-written development texts: training must stay away from them."""
    if not dev_set.exists():
        return []
    return [json.loads(line)["text"] for line in dev_set.read_text(encoding="utf-8").splitlines() if line.strip()]


def clean_rows(rows: list[dict], held: list[str]) -> tuple[list[dict], Counter]:
    """Rows that may be used, and why the others were dropped."""
    others = text_clean.prepared([text_clean.norm(h) for h in held])
    seen: set[str] = set()
    kept, dropped = [], Counter()
    for r in rows:
        key = text_clean.norm(r["text"])
        words = len(r["text"].split())
        if not MIN_WORDS <= words <= MAX_WORDS:
            dropped["too short or too long"] += 1
        elif key in seen:
            dropped["duplicate"] += 1
        elif pii_in(r["text"]):
            dropped["looks like real contact details"] += 1
        elif text_clean.too_close(key, others) >= TOO_CLOSE:
            dropped["too close to the development set"] += 1
        else:
            seen.add(key)
            kept.append(r)
    return kept, dropped


def build(writer: Frontier, labeller: Frontier, sets: sensitivity.ValueSets, cells: list[Cell], salt: str,
          held: list[str]) -> tuple[list[dict], dict]:
    """Write, label blind, keep what both agree on, sort by what the lists catch, clean.
    Returns the rows and a report of what happened to every text asked for."""
    known = set(readable_categories(sets))
    lists = sensitivity.Detector(sets)
    replies = writer.many([(write_messages(c, sets), 1.0, f"{salt}:write:{c.key()}") for c in cells], "written")
    batches = [(c, read_texts(reply)) for c, reply in zip(cells, replies)]
    unread = sum(not texts for _, texts in batches)
    batches = [(c, texts) for c, texts in batches if texts]
    labels = labeller.many([(label_messages(texts, sets), 0, f"{salt}:label:{c.key()}") for c, texts in batches],
                           "labelled blind")
    rows, disagreed, written = [], Counter(), 0
    for (cell, texts), reply in zip(batches, labels):
        got = read_labels(reply, len(texts), known)
        for i, text in enumerate(texts, 1):
            written += 1
            intended = tuple(sorted(cell.categories))
            if got.get(i) != intended:
                disagreed[cell.stratum] += 1
                continue
            caught = {f.category for f in lists.of_text(text)}
            stratum = cell.stratum
            if stratum == "implicit" and set(intended) <= caught:
                stratum = "explicit"                     # the lists already find it: not implied wording
            rows.append({"text": text, "categories": list(intended), "stratum": stratum, "kind": "text",
                         "angle": cell.angle, "about": cell.about, "writer": writer.model["name"],
                         "labeller": labeller.model["name"]})
    kept, dropped = clean_rows(rows, held)
    for n, r in enumerate(kept, 1):
        r["id"] = f"{salt}-{n:05d}"
    report = {"cells": len(cells), "batches_unreadable": unread, "texts_written": written,
              "writer_and_labeller_disagreed": dict(disagreed), "dropped_in_cleaning": dict(dropped), "kept": len(kept),
              "by_stratum": dict(Counter(r["stratum"] for r in kept)),
              "by_category": dict(Counter(c for r in kept for c in r["categories"])),
              "writer": writer.model["name"], "labeller": labeller.model["name"]}
    return kept, report


def split(rows: list[dict], dev_share: float, seed: int = 7) -> tuple[list[dict], list[dict]]:
    """Train and validation rows, split within each (stratum, categories) group so both have every kind."""
    rng = random.Random(seed)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["stratum"], tuple(r["categories"]), r.get("about", "")), []).append(r)
    train, dev = [], []
    for _, members in sorted(groups.items()):
        rng.shuffle(members)
        cut = max(1, round(len(members) * dev_share)) if len(members) > 1 else 0
        dev += members[:cut]
        train += members[cut:]
    return train, dev


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
