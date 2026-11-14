"""Sanitation of machine-written training data, before it can reach a model (ADR 037).

Synthetic data can still carry things a model must not learn: a real-looking phone number or
address, a message copied from a test set, a medication amount in an answer the assistant should
never give. Every row the frontier writes passes here first; what is dropped is counted by reason
and reported in the round's report.

Guard rows (allow / decision / crisis messages) then go through classifier/clean.py's rules too:
duplicates, same template, test-set names, and anything close to a frozen test set, a red-team set,
the confirmed-mistake holdout or a gate set.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from classifier import clean as guard_clean
from classifier.generate import POLICIES, test_names

# Contact details and identifiers. Invented names are expected; these are not.
PII = {
    "email": re.compile(r"\b[\w.+-]+@(?!example\.(?:com|org|net)\b)[\w-]+\.[\w.-]+\b", re.I),
    "phone": re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "long number": re.compile(r"(?<![\d-])\d{9,}(?![\d-])"),
    "street address": re.compile(r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|"
                                 r"Lane|Ln|Drive|Dr|Court|Ct|Way)\b\.?"),
    "url": re.compile(r"https?://(?!(?:www\.)?example\.(?:com|org|net)\b)\S+", re.I),
}
# A medication amount the assistant states as an instruction ("take 10 mg", "increase to 20 mg").
DOSE_INSTRUCTION = re.compile(
    r"\b(?:take|give|start|increase|decrease|reduce|double|raise|lower|switch)\b[^.\n]{0,40}?"
    r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|g|ml|mL|units?|IU|tablets?|pills?)\b"
    r"|\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|ml|mL|units?)\b[^.\n]{0,30}?\b(?:daily|twice|every|per day|a day)\b",
    re.I)


def pii_in(text: str) -> list[str]:
    return [name for name, pattern in PII.items() if pattern.search(text)]


def gives_dose(text: str) -> bool:
    return bool(DOSE_INSTRUCTION.search(text))


def guard_rows(rows: list[dict], own_gate: Path | None = None) -> tuple[list[dict], Counter]:
    """Guard training or gate rows that may be kept, and why the others were dropped. For gate rows,
    `own_gate` is the file they will be written to: an earlier copy of it (the round was run again) is
    not counted as held out against them."""
    shaped, dropped = [], Counter()
    for r in rows:
        text = r.get("text")
        if r.get("profile") not in POLICIES:
            dropped["unknown profile"] += 1
        elif not isinstance(text, str) or not 3 <= len(text.split()) <= 120:
            dropped["empty or too long"] += 1
        elif pii_in(text):
            dropped["contact details or identifiers"] += 1
        else:
            shaped.append(r)
    kept, more = guard_clean.clean(shaped, guard_clean.held_out(skip=own_gate), test_names())
    return kept, dropped + more


def held_out_texts() -> list[str]:
    """Normalized texts every training row must stay away from (classifier/clean.py's list)."""
    return guard_clean.held_out()


def too_close(text: str, held: list[tuple[str, set[str]]]) -> bool:
    return guard_clean.too_close(guard_clean.norm(text), held) >= guard_clean.TOO_CLOSE


def prepare_held(texts: list[str]) -> list[tuple[str, set[str]]]:
    return guard_clean.prepared(texts)
