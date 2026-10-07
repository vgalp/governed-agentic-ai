"""Build clinic.db from Synthea CSV output. Synthetic data only.

    uv run python profiles/clinic-assistant/data/build_db.py --csv <synthea>/output/csv

Data minimization: only the columns the clinic assistant needs are kept.
Synthea's SSN, driver's licence, passport, address, coordinates and income
are dropped, even though they are synthetic.

The output is the same on every run for the same input: rows are inserted in
file order, and invented appointments use a fixed seed and a fixed start date.
"""

import argparse
import csv
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "clinic.db"

# Appointments are invented relative to this date, not today, so every build is identical.
APPOINTMENTS_FROM = datetime(2026, 11, 2, 9, 0)
APPOINTMENT_TYPES = ["Annual physical", "Follow-up visit", "Vaccination", "Lab work", "Consultation"]
SEED = 42

SCHEMA = """
CREATE TABLE patients (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    birth_date  TEXT,
    sex         TEXT,
    city        TEXT
);

CREATE TABLE encounters (
    id          TEXT PRIMARY KEY,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    start       TEXT NOT NULL,          -- ISO timestamp, UTC
    type        TEXT,                   -- wellness, ambulatory, emergency, ...
    description TEXT,
    reason      TEXT,                   -- clinical: nurse role only (step 4)
    total_cost  REAL,
    insurance_paid REAL
);

CREATE TABLE medications (
    id          INTEGER PRIMARY KEY,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    encounter_id TEXT REFERENCES encounters(id),
    start       TEXT NOT NULL,          -- ISO date
    stop        TEXT,                   -- NULL = still taking it
    description TEXT,
    reason      TEXT                    -- clinical: nurse role only
);

CREATE TABLE claims (
    id          TEXT PRIMARY KEY,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    encounter_id TEXT REFERENCES encounters(id),
    service_date TEXT,                  -- ISO date
    amount      REAL,                   -- total for the visit (from encounters)
    insurance_paid REAL,
    patient_status TEXT,                -- CLOSED, BILLED, ...
    patient_outstanding REAL            -- still owed by the patient
);

CREATE TABLE appointments (
    id          TEXT PRIMARY KEY,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    starts_at   TEXT NOT NULL,          -- ISO timestamp, local clinic time
    type        TEXT,
    status      TEXT NOT NULL DEFAULT 'scheduled'
);
"""


def clean_name(s: str) -> str:
    """'Jody426' -> 'Jody' (Synthea adds digits to make names unique)."""
    return re.sub(r"\d+", "", s).strip()


def read_csv(folder: Path, name: str) -> list[dict]:
    with open(folder / f"{name}.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(s: str) -> float | None:
    return float(s) if s else None


def load_patients(db, folder) -> set[str]:
    """Living patients only. Returns their IDs, so other tables can skip the rest."""
    rows = []
    for row in read_csv(folder, "patients"):
        if row["DEATHDATE"]:
            continue
        name = clean_name(row["FIRST"]) + " " + clean_name(row["LAST"])
        rows.append((row["Id"], name, row["BIRTHDATE"], row["GENDER"], row["CITY"]))
    db.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?)", rows)
    print(f"patients:     {len(rows)}")
    return {r[0] for r in rows}


def load_encounters(db, folder, patient_ids: set[str]) -> None:
    rows = [
        (r["Id"], r["PATIENT"], r["START"], r["ENCOUNTERCLASS"], r["DESCRIPTION"],
         r["REASONDESCRIPTION"] or None, to_float(r["TOTAL_CLAIM_COST"]), to_float(r["PAYER_COVERAGE"]))
        for r in read_csv(folder, "encounters")
        if r["PATIENT"] in patient_ids
    ]
    db.executemany("INSERT INTO encounters VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    print(f"encounters:   {len(rows)}")


def load_medications(db, folder, patient_ids: set[str]) -> None:
    rows = [
        (r["PATIENT"], r["ENCOUNTER"] or None, r["START"][:10], r["STOP"][:10] or None,
         r["DESCRIPTION"], r["REASONDESCRIPTION"] or None)
        for r in read_csv(folder, "medications")
        if r["PATIENT"] in patient_ids
    ]
    db.executemany("INSERT INTO medications (patient_id, encounter_id, start, stop, description, reason) "
                   "VALUES (?, ?, ?, ?, ?, ?)", rows)
    print(f"medications:  {len(rows)}")


def load_claims(db, folder, patient_ids: set[str]) -> None:
    """claims.csv has no amounts; they come from the encounter the claim is for."""
    costs = {eid: (total, paid) for eid, total, paid in
             db.execute("SELECT id, total_cost, insurance_paid FROM encounters")}
    rows = []
    for r in read_csv(folder, "claims"):
        if r["PATIENTID"] not in patient_ids or r["APPOINTMENTID"] not in costs:
            continue
        total, paid = costs[r["APPOINTMENTID"]]
        rows.append((r["Id"], r["PATIENTID"], r["APPOINTMENTID"], r["SERVICEDATE"][:10],
                     total, paid, r["STATUSP"] or None, to_float(r["OUTSTANDINGP"])))
    db.executemany("INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    print(f"claims:       {len(rows)}")


def make_appointments(db) -> None:
    """Synthea has no future appointments, so invent 1-3 per patient, the same every run."""
    rng = random.Random(SEED)
    rows = []
    for (patient_id,) in db.execute("SELECT id FROM patients ORDER BY id").fetchall():
        for _ in range(rng.randint(1, 3)):
            day = APPOINTMENTS_FROM + timedelta(days=rng.randint(0, 59))
            while day.weekday() >= 5:            # the clinic is closed on weekends
                day += timedelta(days=1)
            starts_at = day.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
            rows.append((f"apt-{len(rows) + 1:04d}", patient_id,
                         starts_at.isoformat(timespec="minutes"), rng.choice(APPOINTMENT_TYPES)))
    db.executemany("INSERT INTO appointments (id, patient_id, starts_at, type) VALUES (?, ?, ?, ?)", rows)
    print(f"appointments: {len(rows)}")


def build(csv_folder: Path, db_path: Path = DB_PATH) -> Path:
    db_path.unlink(missing_ok=True)          # start clean every run
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")   # reject rows that point at a missing patient or visit
    db.executescript(SCHEMA)
    patient_ids = load_patients(db, csv_folder)
    load_encounters(db, csv_folder, patient_ids)    # before medications and claims, which point at encounters
    load_medications(db, csv_folder, patient_ids)
    load_claims(db, csv_folder, patient_ids)
    make_appointments(db)
    db.commit()
    db.close()
    return db_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", type=Path, required=True, help="Synthea output/csv folder")
    ap.add_argument("--out", type=Path, default=DB_PATH, help="where to write the database")
    args = ap.parse_args()
    print("Built", build(args.csv.expanduser(), args.out.expanduser()))