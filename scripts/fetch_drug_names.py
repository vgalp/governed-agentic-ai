"""Extend privacy/drug_names.txt with every ingredient and brand name RxNorm knows (ADR 024).

    .venv/bin/python scripts/fetch_drug_names.py

Why: the masker (Presidio) takes medication names for people's names ("apixaban" -> [PERSON_1]),
which breaks any question about a drug. The profiles list this file as a masking allow-list, so
a name on it is never masked. The seed list in the repo covers the demo; this script makes it
complete from RxNorm (National Library of Medicine; public, no key). Only the request for the
concept list is sent; nothing about anyone.

Names that are also ordinary words or could be a person's name are kept out by a short stop list,
so the allow-list cannot be used to leak a name through a drug that happens to share it.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "privacy" / "drug_names.txt"
RXNAV = "https://rxnav.nlm.nih.gov/REST/allconcepts.json?tty=IN+BN"
# Brand names that are also first names, surnames or common words: never on the allow-list.
STOP = {"adam", "eve", "ella", "aiden", "alexa", "ada", "lucas", "lilly", "jenny", "lea", "max", "sam", "will",
        "grace", "april", "may", "june", "august", "joy", "hope", "faith", "jade", "amber", "ruby", "rose",
        "lily", "ivy", "iris", "daisy", "holly", "olive", "ginger", "pepper", "sage", "basil", "water", "salt",
        "sugar", "honey", "milk", "oil", "air", "ice", "gold", "silver", "one", "two", "plus", "extra", "new",
        "free", "clear", "pure", "total", "daily", "night", "day", "cold", "flu", "pain", "sleep", "allergy",
        "baby", "children", "child", "adult", "men", "women", "ultra", "super", "mini", "maximum", "regular"}


def main() -> int:
    have = {line.strip().lower() for line in OUT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")} if OUT.exists() else set()
    req = urllib.request.Request(RXNAV, headers={"User-Agent": "governed-agentic-ai/drug-names"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    names = set()
    for c in (data.get("minConceptGroup") or {}).get("minConcept") or []:
        name = (c.get("name") or "").strip().lower()
        # one word, letters only (a hyphen allowed), 4+ characters: multi-word names are not
        # mistaken for people, and short ones would allow-list too much
        if re.fullmatch(r"[a-z][a-z-]{3,}", name) and name not in STOP:
            names.add(name)
    new = names - have
    all_names = sorted(have | names)
    OUT.write_text("# Medication names (generic and brand) that masking must not treat as a person's name.\n"
                   "# From RxNorm (NLM) ingredients and brands, one word each, plus the seed list; "
                   "scripts/fetch_drug_names.py. Lower case; the loader adds the other spellings.\n"
                   + "\n".join(all_names) + "\n", encoding="utf-8")
    print(f"{len(all_names)} names in {OUT.relative_to(ROOT)} ({len(new)} new from RxNorm)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
