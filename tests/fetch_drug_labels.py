"""Download the FDA label sections the medication review needs (ADR 017). Run once, on a machine
with internet access, after building the records database:

    .venv/bin/python scripts/fetch_drug_labels.py                       # PROFILE=clinic-assistant
    .venv/bin/python scripts/fetch_drug_labels.py --profile clinic-assistant

What is sent: only medication names, never anything about a patient.
  1. RxNorm (U.S. National Library of Medicine): each distinct medication name in the records
     database -> its RxNorm code and active ingredients.
  2. openFDA (U.S. Food and Drug Administration): for each ingredient, the most recent
     prescription-drug label that has a drug-interaction section: its set ID, version date, brand
     names, FDA drug classes, and the sections listed in medication_review.yaml.

What is written: the profile's labels file (medication_review.yaml `labels`), with the download
date. FDA labels are U.S. government works (public domain). After this, the medication review
needs no network: it reads this file.
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from profiles.loader import load_profile  # noqa: E402

RXNAV = "https://rxnav.nlm.nih.gov/REST"
OPENFDA = "https://api.fda.gov/drug/label.json"
FIELDS = ["contraindications", "drug_interactions"]


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "governed-agentic-ai/medication-review"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:                      # openFDA: no matching label
                return {}
            if attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(1 + attempt)
    return {}


def rxnorm(name: str) -> tuple[str | None, list[str]]:
    """(RxNorm code, ingredient names) for a medication name."""
    found = get(f"{RXNAV}/approximateTerm.json?" + urllib.parse.urlencode({"term": name, "maxEntries": 1}))
    cands = (found.get("approximateGroup") or {}).get("candidate") or []
    if not cands:
        return None, []
    rxcui = cands[0]["rxcui"]
    rel = get(f"{RXNAV}/rxcui/{rxcui}/related.json?tty=IN")
    names = [p["name"].lower() for g in (rel.get("relatedGroup") or {}).get("conceptGroup") or []
             for p in g.get("conceptProperties") or []]
    return rxcui, sorted(set(names))


def label(ingredient: str, sections: list[str]) -> dict | None:
    """The most recent single-ingredient prescription label for the ingredient that has a
    drug-interaction section (or, failing that, any section we search)."""
    base = [f'openfda.generic_name:"{ingredient}"', 'openfda.product_type:"HUMAN PRESCRIPTION DRUG"']
    for extra in (["_exists_:drug_interactions"], []):
        q = "+AND+".join(urllib.parse.quote(p, safe=':_') for p in base + extra)
        rows = get(f"{OPENFDA}?search={q}&limit=50").get("results") or []
        rows = [r for r in rows if any(r.get(s) for s in sections)]
        if rows:
            break
    if not rows:
        return None

    def single(r):   # a label for this ingredient alone, not a combination product
        g = [x.lower() for x in (r.get("openfda") or {}).get("generic_name", [])]
        return any(ingredient in x and " and " not in x and "," not in x for x in g)
    rows.sort(key=lambda r: (single(r), r.get("effective_time", "")), reverse=True)
    r, o = rows[0], rows[0].get("openfda") or {}
    return {
        "set_id": r["set_id"],
        "label_id": r.get("id"),
        "effective_time": r.get("effective_time"),
        "title": ((o.get("generic_name") or [ingredient])[0]).title(),
        "brand_names": sorted({b.title() for b in o.get("brand_name", [])} - {ingredient.title()}),
        "classes": sorted({c.replace(" [EPC]", "") for c in o.get("pharm_class_epc", [])}),
        "sections": {s: r[s] for s in sections if r.get(s)},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=None, help="profile name (default: PROFILE, or clinic-assistant)")
    args = ap.parse_args()
    import os
    profile = load_profile(args.profile or os.environ.get("PROFILE") or "clinic-assistant")
    cfg_path = profile.path / "medication_review.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    sections = cfg.get("sections", FIELDS)
    rtype = next(r for r in profile.records_map.records if r.name == cfg["medications"])
    if not profile.records_db or not profile.records_db.exists():
        sys.exit(f"Build the records database first: {profile.records_db} (see the profile's data/README.md)")
    with sqlite3.connect(f"file:{profile.records_db}?mode=ro", uri=True) as db:
        names = sorted({n for (n,) in db.execute(f'SELECT DISTINCT "{cfg["drug_column"]}" FROM "{rtype.table}"') if n})

    print(f"{len(names)} medication names in {profile.records_db.name}; looking them up in RxNorm (names only)")
    meds, ingredients = {}, set()
    for n in names:
        rxcui, ings = rxnorm(n)
        meds[n] = {"rxcui": rxcui, "ingredients": ings}
        ingredients |= set(ings)
        print(f"  {'✓' if ings else '?'} {n} -> {', '.join(ings) or 'not found'}")
        time.sleep(0.1)

    print(f"{len(ingredients)} ingredients; downloading FDA label sections from openFDA")
    labels = {}
    for ing in sorted(ingredients):
        lab = label(ing, sections)
        if lab:
            labels[ing] = lab
        print(f"  {'✓' if lab else '–'} {ing}" + (f" ({lab['title']}, {lab['effective_time']})" if lab else ": no label"))
        time.sleep(0.3)

    out = (profile.path / cfg["labels"]).resolve()
    out.write_text(json.dumps({
        "source": "openFDA drug label API, https://open.fda.gov/apis/drug/label/ ; ingredients from RxNorm, "
                  "https://lhncbc.nlm.nih.gov/RxNav/",
        "downloaded": date.today().isoformat(),
        "sections": sections,
        "medications": meds,
        "ingredients": labels,
    }, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {out} ({len(labels)} labels). Restart the pipeline to use them.")


if __name__ == "__main__":
    main()
