"""Measure how much personal data survives masking (the gap named in the production readiness review).

    uv run python -m eval.run_masking                         # the default profile's masking settings
    uv run python -m eval.run_masking --profile clinic-oracle

Each row of eval/datasets/masking_dev_v1.json is an invented sentence with the identifiers written
into it and the medication names that must stay readable. Two views are scored:

    model view    the text after masking (privacy/masking.py): what a model or an outside service sees
    audit view    that text after the audit scrub (privacy.tokens.scrub_for_audit): what is written to
                  the audit trail

An identifier "leaks" in a view when its value is still there, whole. A date of birth is expected in
the model view by design (dates are clinical content and are not masked for a model) and must not be
in the audit view. A medication name masked by mistake is counted too: an answer about [PERSON_3] 5 mg
is no answer.

A DEVELOPMENT set: 150 sentences from ten templates, written by the author of the scrub. It shows
whether masking works on the plain cases and where it does not. It is not evidence about real notes.

Output, in eval/results/ (commit it): masking_dev_v1_<profile>.json and a .md table.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from privacy.tokens import scrub_for_audit

DATASET = Path(__file__).resolve().parent / "datasets" / "masking_dev_v1.json"
RESULTS = Path(__file__).resolve().parent / "results"
TITLES = {"PERSON": "Names", "DATE_OF_BIRTH": "Dates of birth", "PHONE_NUMBER": "Phone numbers",
          "EMAIL_ADDRESS": "Email addresses", "LOCATION": "Street addresses", "US_SSN": "Social security numbers",
          "RECORD_NUMBER": "Record numbers"}


def leaked(value: str, text: str) -> bool:
    """Whether the identifier is still readable: its value whole, or for a number its digits in a row."""
    if value.lower() in text.lower():
        return True
    digits = "".join(c for c in value if c.isdigit())
    return len(digits) >= 6 and digits in "".join(c for c in text if c.isdigit() or c in "[]_ ")


def score(rows: list[dict], mask_text) -> dict:
    """Counts by identifier type for both views, and medication names lost. `mask_text(text) -> masked`."""
    total, in_model, in_audit = Counter(), Counter(), Counter()
    kept = lost = 0
    misses = []
    for r in rows:
        masked = mask_text(r["text"])
        audit = scrub_for_audit(masked)
        for i in r["identifiers"]:
            total[i["type"]] += 1
            in_model[i["type"]] += leaked(i["value"], masked)
            if leaked(i["value"], audit):
                in_audit[i["type"]] += 1
                misses.append({"id": r["id"], "type": i["type"]})
        for word in r.get("keep", []):
            kept += word.lower() in masked.lower()
            lost += word.lower() not in masked.lower()
    return {"rows": len(rows), "identifiers": dict(total), "left_in_model_view": dict(in_model),
            "left_in_audit_view": dict(in_audit), "medication_names_kept": kept, "medication_names_masked": lost,
            "audit_misses": misses}


def table(result: dict) -> str:
    lines = ["| Identifier | In the set | Still readable to a model | Still readable in the audit |", "|---|---|---|---|"]
    for key, title in TITLES.items():
        n = result["identifiers"].get(key, 0)
        if n:
            lines.append(f"| {title} | {n} | {result['left_in_model_view'].get(key, 0)} | "
                         f"{result['left_in_audit_view'].get(key, 0)} |")
    names = result["medication_names_kept"] + result["medication_names_masked"]
    lines += ["", f"Medication names masked by mistake: {result['medication_names_masked']} of {names}.",
              "Dates of birth are left readable to a model by design; they must not be readable in the audit."]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure what survives masking, for a model and for the audit trail.")
    ap.add_argument("--profile", default=None, help="profile whose masking settings to use (default: the active one)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    from privacy.masking import mask  # loads the analyzer (seconds, a spaCy model)
    from profiles.loader import load_profile
    profile = load_profile(args.profile) if args.profile else load_profile()
    rows = json.loads(DATASET.read_text(encoding="utf-8"))
    result = score(rows, lambda text: mask(text, profile)[0])
    md = (f"# Masking: profile {profile.name} on {DATASET.name}\n\nDevelopment set, rerunnable. "
          f"{datetime.now().astimezone().isoformat(timespec='seconds')}.\n\n{table(result)}\n")
    print(md)
    for m in result["audit_misses"][:20]:
        print(f"  still in the audit view: {m['id']} ({m['type']})")
    if not args.no_save:
        path = RESULTS / f"masking_dev_v1_{profile.name}.json"
        path.write_text(json.dumps({"profile": profile.name, "dataset": DATASET.name, **result}, indent=1) + "\n")
        path.with_suffix(".md").write_text(md, encoding="utf-8")
        print(f"\nSaved {path.relative_to(RESULTS.parent.parent)} and the .md table. Commit both.")


if __name__ == "__main__":
    main()
