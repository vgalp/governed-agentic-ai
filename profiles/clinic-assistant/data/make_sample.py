"""Cut full Synthea output down to a small sample that is committed for tests and CI.

    uv run python profiles/clinic-assistant/data/make_sample.py --csv <synthea>/output/csv
"""

import argparse
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "sample_csv"
PATIENTS = 3
PER_PATIENT = 5          # most recent medications and encounters kept per patient


def read(folder, name):
    with open(folder / f"{name}.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def write(name, header, rows):
    with open(OUT / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, header)
        w.writeheader()
        w.writerows(rows)
    print(f"{name + '.csv':18} {len(rows)} rows")


def latest(rows, key, n):
    return sorted(rows, key=lambda r: r[key])[-n:]


def main(folder):
    p_head, patients = read(folder, "patients")
    e_head, encounters = read(folder, "encounters")
    m_head, medications = read(folder, "medications")
    c_head, claims = read(folder, "claims")

    # 1. Pick 3 living patients with medications, plus 1 deceased one (to test it is skipped).
    with_meds = {m["PATIENT"] for m in medications}
    living = sorted((p for p in patients if not p["DEATHDATE"] and p["Id"] in with_meds), key=lambda p: p["Id"])
    chosen = {p["Id"] for p in living[:PATIENTS]}
    deceased = sorted((p for p in patients if p["DEATHDATE"]), key=lambda p: p["Id"])[:1]
    keep_patients = [p for p in patients if p["Id"] in chosen] + deceased

    # 2. Their latest medications, the visits those came from, and their latest visits.
    keep_meds, keep_enc_ids = [], set()
    for pid in sorted(chosen):
        meds = latest([m for m in medications if m["PATIENT"] == pid], "START", PER_PATIENT)
        keep_meds += meds
        keep_enc_ids |= {m["ENCOUNTER"] for m in meds}
        keep_enc_ids |= {e["Id"] for e in latest([e for e in encounters if e["PATIENT"] == pid], "START", PER_PATIENT)}

    # 3. Keep the original file order, and only claims for visits we kept.
    keep_enc = [e for e in encounters if e["Id"] in keep_enc_ids]
    keep_meds = [m for m in medications if m in keep_meds]
    keep_claims = [c for c in claims if c["APPOINTMENTID"] in keep_enc_ids]

    OUT.mkdir(exist_ok=True)
    write("patients", p_head, keep_patients)
    write("encounters", e_head, keep_enc)
    write("medications", m_head, keep_meds)
    write("claims", c_head, keep_claims)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True)
    main(ap.parse_args().csv.expanduser())