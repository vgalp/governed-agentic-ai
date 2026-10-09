"""The records mapping (records.yaml) and the general-purpose records server.

The clinic's records are now built from its mapping, not from clinic code. The proof:
for every patient, role and data class, the output is identical to what the old
clinic-specific server returned (tests/data/records_golden.json, recorded before the
change). The rest checks that a role's hidden columns are never read, that names in
the mapping can't carry SQL, and that a different organization's data works too."""

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from profiles.loader import ProfileError, load_profile, load_profile_from
from profiles.records_map import MappingError, parse

DATA = Path(__file__).resolve().parent.parent / "profiles" / "clinic-assistant" / "data"
GOLDEN = Path(__file__).resolve().parent / "data" / "records_golden.json"
_spec = importlib.util.spec_from_file_location("build_db", DATA / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)

server = pytest.importorskip("mcp_servers.records.server")
CLINIC = load_profile("clinic-assistant")
MAP = CLINIC.records_map


@pytest.fixture
def db(tmp_path):
    return build_db.build(DATA / "sample_csv", tmp_path / "clinic.db")


# --- the clinic: same records as before, from the mapping ---------------------------

def test_mapping_gives_exactly_the_old_records(db):
    """70 recorded cases: every patient, every role, every data class on its own,
    last-name lookups, an ambiguous name, no name, no access."""
    cases = json.loads(GOLDEN.read_text())
    assert len(cases) == 70
    for c in cases:
        assert server.search(db, c["query"], c["classes"], MAP) == c["result"], (c["query"], c["view"])


def test_mapping_matches_the_database(db):
    with sqlite3.connect(db) as conn:
        assert server.check_schema(conn, MAP) == []


def test_every_cited_id_matches_the_profile_pattern(db):
    names = [n for (n,) in sqlite3.connect(db).execute("SELECT name FROM patients")]
    for name in names:
        for r in server.search(db, f"Tell me about {name}", list(CLINIC.data_classes), MAP)["results"]:
            assert CLINIC.source_id_pattern.fullmatch(r["id"]), r["id"]


def _sql_for(db, monkeypatch, classes):
    """Every SQL statement the server runs for one search."""
    seen, real = [], sqlite3.connect

    def traced(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(server.sqlite3, "connect", traced)
    name = real(db).execute("SELECT name FROM patients ORDER BY name").fetchone()[0]
    server.search(db, f"Tell me about {name}", classes, MAP)
    return seen


def test_front_desk_never_reads_clinical_columns(db, monkeypatch):
    sql = _sql_for(db, monkeypatch, list(CLINIC.roles["front_desk"].data_classes))
    visits = [s for s in sql if '"encounters"' in s]
    assert visits and all('"reason"' not in s and '"description"' not in s for s in visits)
    assert not any('"medications"' in s for s in sql)


def test_nurse_never_reads_claims(db, monkeypatch):
    sql = _sql_for(db, monkeypatch, list(CLINIC.roles["nurse"].data_classes))
    assert not any('"claims"' in s for s in sql)


def test_values_are_parameters_not_sql(db):
    """A name with a quote in it is just text."""
    assert server.search(db, "When is O'Brien'; DROP TABLE patients; --'s visit?",
                         list(CLINIC.data_classes), MAP)["results"] == []
    assert sqlite3.connect(db).execute("SELECT count(*) FROM patients").fetchone()[0] == 3


def test_schema_mismatch_is_reported(db):
    raw = yaml.safe_load((CLINIC.path / "records.yaml").read_text())
    raw["records"]["claims"]["table"] = "invoices"
    raw["records"]["medications"]["text"] = "{subject}: {drug_name}."
    m = parse(raw, "records.yaml", set(CLINIC.data_classes))
    with sqlite3.connect(db) as conn:
        problems = server.check_schema(conn, m)
    assert "table 'invoices' is not in the database" in problems
    assert "column 'medications.drug_name' is not in the database" in problems


# --- another organization's data, same code -------------------------------------------

LIBRARY = {
    "subject": {"noun": "member", "table": "members", "id": "member_no", "name": "full_name",
                "record": {"source_id": {"prefix": "mem-"}, "data_class": "contact",
                           "title": "Member: {full_name}", "text": "{full_name}, member since {joined|date}."}},
    "records": {"loans": {"table": "loans", "link": "member_no", "id": "loan_no",
                          "source_id": {"prefix": "loan-"}, "data_class": "loans",
                          "only_where": {"returned": 0}, "order_by": ["due desc"],
                          "title": "Book on loan", "text": "{subject} has {title}, due {due|date}.",
                          "optional": {"fine": " Fine owed: {fine|money}."}}},
}


@pytest.fixture
def library(tmp_path):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE members (member_no INTEGER PRIMARY KEY, full_name TEXT, joined TEXT, address TEXT);
        CREATE TABLE loans (loan_no INTEGER PRIMARY KEY, member_no INTEGER, title TEXT,
                            due TEXT, returned INTEGER, fine REAL);
        INSERT INTO members VALUES (7, 'Ada Byron', '2024-02-01T10:00', '1 Secret Lane');
        INSERT INTO loans VALUES (1, 7, 'Dune', '2026-10-01', 0, 2.5),
                                 (2, 7, 'Emma', '2026-10-20', 0, NULL),
                                 (3, 7, 'Ulysses', '2026-09-01', 1, NULL);
    """)
    conn.commit()
    conn.close()
    return path


def test_a_different_database_works_from_its_mapping(library):
    m = parse(LIBRARY, "records.yaml", {"contact", "loans"})
    with sqlite3.connect(library) as conn:
        assert server.check_schema(conn, m) == []
    out = server.search(library, "What does Ada Byron have out?", ["contact", "loans"], m)["results"]
    assert [(r["id"], r["text"]) for r in out] == [
        ("mem-7", "Ada Byron, member since 2024-02-01."),
        ("loan-2", "Ada Byron has Emma, due 2026-10-20."),
        ("loan-1", "Ada Byron has Dune, due 2026-10-01. Fine owed: 2.50."),
    ]
    assert all("Secret Lane" not in r["text"] for r in out)        # unmapped columns are never shown


def test_notes_use_the_profiles_own_word(library):
    m = parse(LIBRARY, "records.yaml", {"contact", "loans"})
    assert server.search(library, "Ada Byron", [], m)["note"] == "this role may not see member records"


# --- what the mapping refuses -------------------------------------------------------------

def bad(change):
    raw = json.loads(json.dumps(LIBRARY))
    change(raw)
    return parse(raw, "records.yaml", {"contact", "loans"})


@pytest.mark.parametrize("change, message", [
    (lambda r: r["subject"].update(table="members; DROP TABLE members"), "must be a table or column name"),
    (lambda r: r["records"]["loans"].update(order_by=["due; DELETE"]), "must be 'column' or 'column desc'"),
    (lambda r: r["records"]["loans"].update(text="{subject} owes {fine:.2f}"), "not Python format codes"),
    (lambda r: r["records"]["loans"].update(text="{subject} has {title|upper}"), "unknown filter 'upper'"),
    (lambda r: r["records"]["loans"].update(data_class="secrets"), "data class 'secrets' is not in roles.yaml"),
    (lambda r: r["records"]["loans"].update(only_where={"returned": [0, 1]}), "single value or null"),
    (lambda r: r["records"]["loans"].update(limit=0), "positive whole number"),
    (lambda r: r["records"]["loans"].pop("link"), "missing 'link'"),
    (lambda r: r["records"]["loans"]["source_id"].update(prefix="LOAN_"), "should look like"),
    (lambda r: r["subject"]["record"].update(text="{subject} joined"), "only for record texts"),
    (lambda r: r["records"]["loans"].update(more_detail={"data_class": "loans", "text": "x"}),
     "needs a different data class"),
])
def test_bad_mappings_are_refused(change, message):
    with pytest.raises(MappingError, match=message):
        bad(change)


# --- the profile ties the mapping to roles --------------------------------------------------

@pytest.fixture
def clinic_copy(tmp_path):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(CLINIC.path, dst, ignore=shutil.ignore_patterns("clinic.db", "sample_csv"))

    def edit(name, change):
        p = dst / name
        d = yaml.safe_load(p.read_text())
        change(d)
        p.write_text(yaml.safe_dump(d, sort_keys=False))
        return load_profile_from(dst)
    return edit


def test_profile_mapping_classes_must_be_in_roles(clinic_copy):
    with pytest.raises(ProfileError, match="data class 'lab_results' is not in roles.yaml"):
        clinic_copy("records.yaml", lambda d: d["records"]["visits"].update(data_class="lab_results"))


def test_records_db_needs_a_mapping(clinic_copy):
    with pytest.raises(ProfileError, match="records_db and records_map go together"):
        clinic_copy("profile.yaml", lambda d: d.pop("records_map"))