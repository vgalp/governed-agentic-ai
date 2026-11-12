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
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_servers.medication_review.labels import FIELDS, OPENFDA, RXNAV, get, label, rxcui_ingredients, rxnorm  # noqa: E402,F401
from profiles.loader import load_profile  # noqa: E402


def medications_from_fhir(profile, cfg: dict, patients: list[str]) -> list[tuple[str, str | None]]:
    """(name, RxNorm code) of the current medications of the named patients, read from the FHIR
    server (ADR 013). Patient data stays here: only drug names and codes go to RxNorm and openFDA."""
    from fhir.client import client_from_settings
    from mcp_servers.common.patients import NotFound, Source
    from mcp_servers.medication_review.server import load_config, medications_of
    src = Source.open(profile, "fhir", client=client_from_settings(profile.fhir_map.server))
    review_cfg = load_config(profile)
    out = []
    for p in patients:
        patient = src.find(f"medications for {p}")
        if isinstance(patient, NotFound):
            print(f"  ? {p}: {patient.reply}")
            continue
        meds = medications_of(src, review_cfg, patient)
        print(f"  ✓ {patient.name}: {len(meds)} current medication(s)")
        out += [(x["name"], x.get("rxcui")) for x in meds]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=None, help="profile name (default: PROFILE, or clinic-assistant)")
    ap.add_argument("--patient", action="append", default=[],
                    help="FHIR profiles: a test patient whose medications to cover (repeat for several)")
    args = ap.parse_args()
    import os

    from scripts.fhir_check import load_env  # client IDs and keys from .env, as dev.sh does
    load_env(Path(__file__).resolve().parent.parent / ".env")
    profile = load_profile(args.profile or os.environ.get("PROFILE") or "clinic-assistant")
    cfg_path = profile.path / "medication_review.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    sections = cfg.get("sections", FIELDS)
    out = (profile.path / cfg["labels"]).resolve()
    # A FHIR profile adds to what earlier runs downloaded (other test patients); a database profile
    # covers every medication in the database, so it starts fresh.
    previous = json.loads(out.read_text()) if out.exists() and cfg.get("source") == "fhir" else {}

    if cfg.get("source") == "fhir":
        if not args.patient:
            sys.exit('Name the test patients with dates of birth: --patient "Camila Lopez 1987-09-12"')
        print(f"Reading current medications from {profile.fhir_map.server['base_url']}")
        coded = medications_from_fhir(profile, cfg, args.patient)
    else:
        rtype = next(r for r in profile.records_map.records if r.name == cfg["medications"])
        if not profile.records_db or not profile.records_db.exists():
            sys.exit(f"Build the records database first: {profile.records_db} (see the profile's data/README.md)")
        with sqlite3.connect(f"file:{profile.records_db}?mode=ro", uri=True) as db:
            coded = [(n, None) for (n,) in db.execute(
                f'SELECT DISTINCT "{cfg["drug_column"]}" FROM "{rtype.table}"') if n]

    print(f"{len(coded)} medication names; looking them up in RxNorm (names and codes only)")
    meds, by_code, ingredients = dict(previous.get("medications") or {}), dict(previous.get("rxcui") or {}), set()
    for n, code in sorted(set(coded), key=lambda x: x[0]):
        if code:
            ings = rxcui_ingredients(code)
            by_code[code] = ings
        else:
            code, ings = rxnorm(n)
        meds[n] = {"rxcui": code, "ingredients": ings}
        ingredients |= set(ings)
        print(f"  {'✓' if ings else '?'} {n} -> {', '.join(ings) or 'not found'}")
        time.sleep(0.1)

    print(f"{len(ingredients)} ingredients; downloading FDA label sections from openFDA")
    labels = dict(previous.get("ingredients") or {})
    for ing in sorted(ingredients):
        lab = label(ing, sections)
        if lab:
            labels[ing] = lab
        print(f"  {'✓' if lab else '–'} {ing}" + (f" ({lab['title']}, {lab['effective_time']})" if lab else ": no label"))
        time.sleep(0.3)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "source": "openFDA drug label API, https://open.fda.gov/apis/drug/label/ ; ingredients from RxNorm, "
                  "https://lhncbc.nlm.nih.gov/RxNav/",
        "downloaded": date.today().isoformat(),
        "sections": sections,
        "medications": meds,
        "rxcui": by_code,
        "ingredients": labels,
    }, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {out} ({len(labels)} labels). Restart the pipeline to use them.")


if __name__ == "__main__":
    main()
