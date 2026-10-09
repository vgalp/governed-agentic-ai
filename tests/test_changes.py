"""The generic change server (mcp_servers/changes): each profile describes its changes
in actions.yaml (change:), with no code. The clinic moves appointments, the county
releases held payments, on the same server. All data is synthetic."""

import json
import shutil
import sqlite3

import pytest
import yaml

from profiles.loader import ProfileError, load_profile, load_profile_from

server = pytest.importorskip("mcp_servers.changes.server")

COUNTY = load_profile("county-benefits")
RELEASE = COUNTY.actions["release_payment"]


@pytest.fixture
def county_db(tmp_path):
    path = tmp_path / "county.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((COUNTY.path / "data" / "seed.sql").read_text())
    return path


def payment_status(db, pid):
    return sqlite3.connect(db).execute("SELECT status FROM payments WHERE id = ?", (pid,)).fetchone()[0]


# --- the county: release a held payment ---------------------------------------------------

def test_the_county_change_is_configuration_only():
    ch = RELEASE.change
    assert (ch.table, ch.id_column, ch.prefix) == ("payments", "id", "pay-")
    assert ch.set_values == (("status", "issued"),) and ch.set_from == ()
    assert ch.only_if == (("status", "held"),)
    assert not list(COUNTY.path.rglob("*.py"))


def test_schema_matches_for_every_profile(county_db):
    with sqlite3.connect(county_db) as conn:
        assert server.check_changes(conn, COUNTY) == []


def test_release_a_held_payment(county_db):
    r = server.apply(county_db, RELEASE, {"payment_id": "pay-5004"}, "ap-1")
    assert r["status"] == "done" and r["record_id"] == "pay-5004"
    assert r["before"] == {"status": "held"} and r["after"] == {"status": "issued"}
    assert r["summary"] == "pay-5004 released: status held -> issued"
    assert payment_status(county_db, 5004) == "issued"


def test_only_a_held_payment_is_released(county_db):
    r = server.apply(county_db, RELEASE, {"payment_id": "pay-5003"}, "ap-1")   # scheduled
    assert r["status"] == "failed" and r["reason"] == "pay-5003 can't be changed: status is scheduled, not held"
    assert payment_status(county_db, 5003) == "scheduled"


def test_a_release_happens_once(county_db):
    server.apply(county_db, RELEASE, {"payment_id": "pay-5004"}, "ap-1")
    again = server.apply(county_db, RELEASE, {"payment_id": "pay-5004"}, "ap-1")
    assert again["status"] == "already_done" and again["summary"] == "pay-5004 released: status held -> issued"
    other = server.apply(county_db, RELEASE, {"payment_id": "pay-5004"}, "ap-2")   # a second approval
    assert other["status"] == "failed" and "status is issued, not held" in other["reason"]
    rows = sqlite3.connect(county_db).execute("SELECT approval_id, target, before, after FROM action_log").fetchall()
    assert rows == [("ap-1", "pay-5004", json.dumps({"status": "held"}), json.dumps({"status": "issued"}))]


@pytest.mark.parametrize("fields, reason", [
    ({"payment_id": "pay-9999"}, "pay-9999 not found"),
    ({"payment_id": "inc-4001"}, "does not look right"),         # another record type
    ({"payment_id": "5004"}, "does not look right"),             # a stored ID, not a cited one
    ({}, "no payment_id"),
])
def test_bad_releases_are_refused(county_db, fields, reason):
    r = server.apply(county_db, RELEASE, fields, "ap-1")
    assert r["status"] == "failed" and reason in r["reason"]
    assert payment_status(county_db, 5004) == "held"


def test_extra_fields_are_ignored(county_db):
    """Only the profile decides what is written: a caller can't add a column or a value."""
    r = server.apply(county_db, RELEASE, {"payment_id": "pay-5004", "amount": 9999, "status": "x"}, "ap-1")
    assert r["after"] == {"status": "issued"}
    assert sqlite3.connect(county_db).execute("SELECT amount FROM payments WHERE id = 5004").fetchone() == (650.0,)


def test_values_never_go_into_the_sql(county_db, monkeypatch):
    seen, real = [], sqlite3.connect

    def traced(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(server.sqlite3, "connect", traced)
    server.apply(county_db, RELEASE, {"payment_id": "pay-5004"}, "ap-'; DROP TABLE payments; --")
    assert payment_status(county_db, 5004) == "issued"
    assert sqlite3.connect(county_db).execute("SELECT count(*) FROM payments").fetchone() == (4,)


def test_no_database_no_change():
    r = server.apply(None, RELEASE, {"payment_id": "pay-5004"}, "ap-1")
    assert r["status"] == "failed" and "no records database" in r["reason"]


def test_schema_problems_are_found_at_startup(tmp_path):
    path = tmp_path / "bad.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, amount REAL)")
        assert server.check_changes(conn, COUNTY) == [
            "action release_payment: column 'payments.status' is not in the database"]


# --- the loader refuses changes that could do harm ---------------------------------------

def county_copy(tmp_path, edit):
    dst = tmp_path / "county-benefits"
    shutil.copytree(COUNTY.path, dst, ignore=shutil.ignore_patterns("*.db"))
    data = yaml.safe_load((dst / "actions.yaml").read_text())
    edit(data["release_payment"])
    (dst / "actions.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return load_profile_from(dst)


def test_the_unedited_copy_loads(tmp_path):
    assert county_copy(tmp_path, lambda a: None).actions["release_payment"].change is not None


@pytest.mark.parametrize("edit, error", [
    (lambda a: a["change"].update(record="nope"), "not a record type"),
    (lambda a: a["change"].update(id_field="nope"), "must be a field of type source"),
    (lambda a: a["change"].update(set={"id": 1}), "can never be changed"),
    (lambda a: a["change"].update(set={"applicant_id": 1001}), "can never be changed"),
    (lambda a: a["change"].update(set={}), "writes nothing"),
    (lambda a: a["change"].update(set={"status; DROP TABLE x": "issued"}), "must be a column name"),
    (lambda a: a["change"].update(set={"status": ["issued"]}), "single value"),
    (lambda a: a["change"].update(set_from={"amount": "payment_id"}), "not the ID field"),
    (lambda a: a.update(done_text="{id} {before_amount}"), "is not one of"),
    (lambda a: a.update(done_text="{id|shout}"), "unknown filter"),
    (lambda a: a.pop("change"), "done_text needs a change block"),
])
def test_bad_change_blocks_are_refused(tmp_path, edit, error):
    with pytest.raises(ProfileError, match=error):
        county_copy(tmp_path, edit)


def test_a_record_with_shortened_ids_cannot_be_changed(tmp_path):
    dst = tmp_path / "county-benefits"
    shutil.copytree(COUNTY.path, dst, ignore=shutil.ignore_patterns("*.db"))
    rec = yaml.safe_load((dst / "records.yaml").read_text())
    rec["records"]["payments"]["source_id"]["keep_last"] = 2
    (dst / "records.yaml").write_text(yaml.safe_dump(rec, sort_keys=False))
    with pytest.raises(ProfileError, match="keep_last"):
        load_profile_from(dst)


def test_one_tool_per_action(tmp_path):
    """Each change has its own tool, so OPA, roles and the audit see every change by name."""
    dst = tmp_path / "county-benefits"
    shutil.copytree(COUNTY.path, dst, ignore=shutil.ignore_patterns("*.db"))
    data = yaml.safe_load((dst / "actions.yaml").read_text())
    data["release_payment_again"] = dict(data["release_payment"])
    (dst / "actions.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(ProfileError, match="one tool per action"):
        load_profile_from(dst)


def test_the_clinic_keeps_its_rule_only_scheduled_appointments_move():
    ch = load_profile("clinic-assistant").actions["reschedule_appointment"].change
    assert ch.set_from == (("starts_at", "new_start"),) and ch.only_if == (("status", "scheduled"),)