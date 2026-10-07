"""Roles: each role gets only its data classes, the gateway re-checks, and broken
role files are rejected. The OPA side is tested with: opa test policies/ -v"""

import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from profiles.loader import ProfileError, load_profile, load_profile_from

DATA = Path(__file__).resolve().parent.parent / "profiles" / "clinic-assistant" / "data"
_spec = importlib.util.spec_from_file_location("build_db", DATA / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)

server = pytest.importorskip("mcp_servers.records.server")
gateway = pytest.importorskip("gateway.gateway")

CLINIC = load_profile("clinic-assistant")


@pytest.fixture
def db(tmp_path):
    return build_db.build(DATA / "sample_csv", tmp_path / "clinic.db")


def records_for(db, role):
    name = sqlite3.connect(db).execute("SELECT name FROM patients ORDER BY name").fetchone()[0]
    classes = list(CLINIC.roles[role].data_classes)
    return server.search(db, f"Tell me about {name}", classes)["results"]


def kinds(results):
    return {r["id"].split("-")[0] for r in results}


# --- what each role gets from the records server ------------------------------

def test_clinician_sees_everything(db):
    assert kinds(records_for(db, "clinician")) >= {"pt", "med", "enc", "clm"}


def test_front_desk_never_gets_medications_or_visit_reasons(db):
    results = records_for(db, "front_desk")
    assert "med" not in kinds(results)
    visits = [r for r in results if r["id"].startswith("enc-")]
    assert visits and all(r["data_classes"] == ["visit_dates"] for r in visits)
    assert all("Reason recorded" not in r["text"] for r in results)


def test_nurse_never_gets_claims(db):
    assert "clm" not in kinds(records_for(db, "nurse"))


def test_compliance_gets_no_patient_records(db):
    assert records_for(db, "compliance") == []


def test_every_record_lists_its_data_classes(db):
    assert all(r["data_classes"] for r in records_for(db, "clinician"))


# --- the gateway checks again --------------------------------------------------

def test_gateway_drops_records_with_classes_the_role_may_not_see():
    results = [{"id": "a", "data_classes": ["appointments"]},
               {"id": "b", "data_classes": ["appointments", "medications"]},
               {"id": "c"}]                                   # no classes: fail closed
    kept, dropped = gateway.filter_results(results, ["appointments"])
    assert [r["id"] for r in kept] == ["a"] and dropped == 2


def test_gateway_passes_allowed_classes_and_audits_the_role(monkeypatch):
    events, sent = [], {}
    monkeypatch.setattr(gateway, "_is_allowed", lambda p, a, t, r: True)
    monkeypatch.setattr(gateway, "allowed_classes", lambda p, r: ["appointments"])
    monkeypatch.setattr(gateway, "_audit", events.append)

    async def fake_call(url, tool, args):
        sent.update(args)
        return {"results": [{"id": "apt-1", "data_classes": ["appointments"]},
                            {"id": "med-1", "data_classes": ["medications"]}]}   # tool over-returns
    monkeypatch.setattr(gateway, "_call", fake_call)
    out = gateway.call_tool("planner", "search_records", {"query": "Ann Lee"}, "r1", CLINIC,
                            audit_args={"query": "[PERSON_1]"}, role="front_desk")
    assert sent["allowed_classes"] == ["appointments"]
    assert [r["id"] for r in out["results"]] == ["apt-1"]
    e = events[-1]
    assert e["role"] == "front_desk" and e["dropped_by_gateway"] == 1 and e["returned"] == 1
    assert e["args"]["query"] == "[PERSON_1]"                 # still masked in the audit


def test_denied_role_raises_and_is_audited(monkeypatch):
    events = []
    monkeypatch.setattr(gateway, "_is_allowed", lambda p, a, t, r: False)
    monkeypatch.setattr(gateway, "_audit", events.append)
    with pytest.raises(gateway.ToolCallDenied):
        gateway.call_tool("planner", "search_records", {"query": "x"}, "r1", CLINIC,
                          audit_args={"query": "x"}, role="compliance")
    assert events[-1]["decision"] == "deny" and events[-1]["role"] == "compliance"


# --- the profile ---------------------------------------------------------------

def test_clinic_roles_and_policy_data():
    assert set(CLINIC.roles) == {"clinician", "nurse", "front_desk", "compliance"}
    assert CLINIC.role_filtered_tools == ("search_records",)
    assert CLINIC.policy_data()["roles"]["compliance"]["tools"] == ["search_knowledge"]
    assert "role_required" in CLINIC.responses


def test_profile_without_roles_has_none():
    p = load_profile("adhd-assistant")
    assert p.roles == {} and "roles" not in p.policy_data()


@pytest.fixture
def copy_of_clinic(tmp_path):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(CLINIC.path, dst, ignore=shutil.ignore_patterns("clinic.db", "sample_csv"))
    return dst


def _edit(path, change):
    d = yaml.safe_load(path.read_text())
    change(d)
    path.write_text(yaml.safe_dump(d, sort_keys=False))


def test_unknown_data_class_is_rejected(copy_of_clinic):
    _edit(copy_of_clinic / "roles.yaml", lambda d: d["roles"]["nurse"]["data_classes"].append("everything"))
    with pytest.raises(ProfileError, match="unknown data class 'everything'"):
        load_profile_from(copy_of_clinic)


def test_role_tool_must_be_granted(copy_of_clinic):
    _edit(copy_of_clinic / "roles.yaml", lambda d: d["roles"]["nurse"]["tools"].append("delete_records"))
    with pytest.raises(ProfileError, match="'delete_records' is not granted"):
        load_profile_from(copy_of_clinic)


def test_roles_need_a_role_required_reply(copy_of_clinic):
    _edit(copy_of_clinic / "responses.yaml", lambda d: d.pop("role_required"))
    with pytest.raises(ProfileError, match="role_required"):
        load_profile_from(copy_of_clinic)



# --- replies worded for the role ------------------------------------------------

def test_clinician_gets_clinician_wording_for_the_same_block():
    clinician = CLINIC.response("medical_advice", "clinician")
    default = CLINIC.response("medical_advice", "nurse")
    assert clinician != default and "your clinical judgment" in clinician
    assert default == CLINIC.responses["medical_advice"]


def test_unknown_or_missing_role_gets_the_default_reply():
    assert CLINIC.response("medication", "visitor") == CLINIC.responses["medication"]
    assert CLINIC.response("medication") == CLINIC.responses["medication"]


def test_role_reply_must_be_a_known_category(copy_of_clinic):
    _edit(copy_of_clinic / "roles.yaml",
          lambda d: d["roles"]["nurse"].setdefault("responses", {}).update(anything="Hi"))
    with pytest.raises(ProfileError, match="not a category in responses.yaml"):
        load_profile_from(copy_of_clinic)