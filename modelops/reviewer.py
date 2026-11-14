"""The frontier reviewer: settles the messages the two labellers disagree on, so a person sees only what it
is unsure about (ADR 037).

For each message it is shown the assistant's policy, the label rules, the label the message was written for
and the label the local model gave instead, and returns a label, its confidence, a one-line reason, and
whether a person needs to look. Then:

    high confidence, same label as written                   → accepted: trained on
    high confidence, a different label, not crisis           → rejected: dropped (the message missed its aim)
    reviewer says crisis where it was not written as crisis  → flagged: a person looks (possible missed risk)
    low confidence, needs_person, or no readable review      → flagged: a person looks

The reason is kept in reviewed.csv for the record. It is not a doubt: reviewers explain their labels.

The reviewer also checks a sample of the messages both labellers agreed on (`reviewer_sample_rate`); one it
doubts is taken out of training and flagged. Every decision is written to reviewed.csv in the round folder,
so what the reviewer settled can be audited later; only flagged rows go to audit.csv for a person.

The reviewer is the frontier model by default. A different model (loop.yaml `reviewer:`) is more independent
of the writer, and is the better choice once you have access to one.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from classifier.generate import HEALTH, HEALTH_NOTE, LABEL_RULES, POLICIES
from modelops.frontier import Frontier, parse_json

BATCH = 10
LABELS = ("allow", "decision", "crisis")
AUDIT_FIELDS = ["id", "why", "profile", "kind", "label", "local_label", "reviewer_label", "reviewer_confidence",
                "needs_person", "concern", "text", "your_label", "note"]


def review_messages(profile: str, rows: list[dict]) -> list[dict]:
    health = f"\n{HEALTH_NOTE}" if profile in HEALTH else ""
    listed = "\n".join(f"{i + 1}. [written as {r['label']}; second labeller said {r.get('local_label') or 'nothing'}] "
                       f"{r['text']}" for i, r in enumerate(rows))
    return [
        {"role": "system", "content": "You review labels for a safety classifier that protects staff and the people "
                                      "they serve. Be careful, not quick. Reply with JSON only."},
        {"role": "user", "content":
            f"The assistant: {POLICIES[profile]}\n\n{LABEL_RULES}{health}\n\n"
            f"Each message below was written to have one label; a second labeller may have disagreed. Decide the "
            f"correct label for each, with one short sentence of reason. Say confidence \"high\" only when the policy "
            f"and rules clearly decide it; otherwise \"low\". Set needs_person to true only when a person really must "
            f"look: the risk is genuinely ambiguous, it could be read two ways that change the label, or it seems to "
            f"contain a real person's details. Explaining your label is not a reason to set it.\n\n"
            f"{listed}\n\n"
            'Reply as JSON: {"reviews": [{"n": 1, "label": "allow" | "decision" | "crisis", '
            f'"confidence": "high" | "low", "reason": "...", "needs_person": false}}, ...]}}, '
            f"exactly {len(rows)} reviews."},
    ]


NO_CONCERN = re.compile(r"^(?:none|n/?a|no|nil|null|nothing|-+|—|no concerns?(?: here)?|no issues?|not applicable|"
                        r"none needed|no concern needed|clear(?:ly \w+)?)\.?$", re.I)


def real_concern(value) -> str:
    """The concern text, or "" when the reviewer wrote a placeholder such as "None" or "N/A"."""
    text = str(value or "").strip()
    return "" if not text or NO_CONCERN.match(text) else text[:300]


def decide(row: dict, review: dict | None) -> tuple[str, dict]:
    """(accepted | rejected | flagged, the row with the reviewer's answer and the reason).

    Doubt is read from `confidence` and `needs_person`. The reviewer's `reason` (or, from reviews made before
    needs_person existed, `concern`) explains its label and is kept for the record; it does not by itself send
    the message to a person, because reviewers use that field to explain, not to doubt."""
    if not isinstance(review, dict) or review.get("label") not in LABELS:
        return "flagged", {**row, "why": "no usable review"}
    reason = real_concern(review.get("reason") or review.get("concern"))
    out = {**row, "reviewer_label": review["label"], "reviewer_confidence": str(review.get("confidence", "")),
           "concern": reason, "needs_person": str(_truthy(review.get("needs_person"))).lower()}
    if out["reviewer_confidence"].lower() != "high":
        return "flagged", {**out, "why": "reviewer unsure"}
    if _truthy(review.get("needs_person")):
        return "flagged", {**out, "why": "reviewer asks for a person"}
    if review["label"] == row["label"]:
        return "accepted", {**out, "why": "settled by reviewer"}
    if review["label"] == "crisis":
        return "flagged", {**out, "why": "reviewer sees crisis it was not written as"}
    return "rejected", {**out, "why": "reviewer: a different label"}


def _truthy(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"))


def review(frontier: Frontier, rows: list[dict], salt: str) -> dict[str, list[dict]]:
    """Review rows in batches per profile; {"accepted": [...], "rejected": [...], "flagged": [...]}."""
    by_profile: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_profile[r["profile"]].append(r)
    chunks = [(p, items[i:i + BATCH]) for p, items in by_profile.items() for i in range(0, len(items), BATCH)]
    out: dict[str, list[dict]] = {"accepted": [], "rejected": [], "flagged": []}
    if not chunks:
        return out
    replies = frontier.many([(review_messages(p, c), 0, f"{salt}:review") for p, c in chunks], "reviewed")
    for (_, chunk), reply in zip(chunks, replies):
        reviews = read_reviews(reply, len(chunk))
        for i, row in enumerate(chunk, start=1):
            verdict, decided = decide(row, reviews.get(i))
            out[verdict].append(decided)
    return out


def read_reviews(reply: str, n: int) -> dict[int, dict]:
    """The reviews by message number. Accepts "n" as a number or text, a bare list, or reviews in order
    without numbers when there are exactly n of them; labels and confidence in any case."""
    obj = parse_json(reply)
    items = obj.get("reviews") if obj else None
    if items is None:
        m = re.search(r"\[.*\]", reply, re.S)
        try:
            items = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            items = None
    items = [r for r in items or [] if isinstance(r, dict)]
    out: dict[int, dict] = {}
    for r in items:
        try:
            k = int(str(r.get("n", "")).strip().rstrip("."))
        except ValueError:
            continue
        out[k] = r
    if not out and len(items) == n:
        out = dict(enumerate(items, start=1))
    for r in out.values():
        if isinstance(r.get("label"), str):
            r["label"] = r["label"].strip().lower()
        if isinstance(r.get("confidence"), str):
            r["confidence"] = r["confidence"].strip().lower()
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str] = AUDIT_FIELDS) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({**{k: "" for k in fields}, **r} for r in rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))
