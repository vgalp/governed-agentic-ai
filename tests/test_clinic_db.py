"""The clinic database builds correctly from the committed Synthea sample."""

import csv
import importlib.util
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "profiles" / "clinic-assistant" / "data"
SAMPLE = DATA / "sample_csv"
TABLES = ["patients", "encounters", "medications", "claims", "appointments"]

# The folder name has a hyphen, so load build_db.py by its file path.
_spec = importlib.util.spec_from_file_location("build_db", DATA / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)


def all_rows(db_path):
    db = sqlite3.connect(db_path)
    try:
        return {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}
    finally:
        db.close()


def test_every_table_has_rows(tmp_path):
    rows = all_rows(build_db.build(SAMPLE, tmp_path / "clinic.db"))
    for table in TABLES:
        assert rows[table], f"{table} is empty"


def test_every_link_points_at_a_real_row(tmp_path):
    db = sqlite3.connect(build_db.build(SAMPLE, tmp_path / "clinic.db"))
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_two_builds_are_identical(tmp_path):
    a = all_rows(build_db.build(SAMPLE, tmp_path / "a.db"))
    b = all_rows(build_db.build(SAMPLE, tmp_path / "b.db"))
    assert a == b


def test_names_are_clean(tmp_path):
    rows = all_rows(build_db.build(SAMPLE, tmp_path / "clinic.db"))
    for _id, name, *_ in rows["patients"]:
        assert not re.search(r"\d", name), name


def test_deceased_patients_are_left_out(tmp_path):
    with open(SAMPLE / "patients.csv", newline="", encoding="utf-8") as f:
        deceased = {r["Id"] for r in csv.DictReader(f) if r["DEATHDATE"]}
    ids = {r[0] for r in all_rows(build_db.build(SAMPLE, tmp_path / "clinic.db"))["patients"]}
    assert ids and not ids & deceased


def test_only_minimal_columns_are_kept(tmp_path):
    db = sqlite3.connect(build_db.build(SAMPLE, tmp_path / "clinic.db"))
    columns = {c[1] for c in db.execute("PRAGMA table_info(patients)")}
    assert columns == {"id", "name", "birth_date", "sex", "city"}       # no SSN, address, income


def test_appointments_are_on_weekdays(tmp_path):
    rows = all_rows(build_db.build(SAMPLE, tmp_path / "clinic.db"))["appointments"]
    assert all(datetime.fromisoformat(r[2]).weekday() < 5 for r in rows)