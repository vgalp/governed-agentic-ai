"""Chart brief (ADR 023): the record assembled and cited line by line, with documentation checks,
from the database (clinic-assistant) and from a FHIR server (clinic-epic, faked here). No model."""

import re
import sys
from pathlib import Path

import pytest

from mcp_servers.chart_brief import server as cb
from profiles.loader import load_profile

DATA = Path("profiles/clinic-assistant/data")
sys.path.insert(0, str(DATA))
import build_db  # noqa: E402

P = load_profile("clinic-assistant")
CFG = cb.load_config(P)
CLINICIAN = list(P.roles["clinician"].data_classes)
NURSE = list(P.roles["nurse"].data_classes)
FRONT = list(P.roles["front_desk"].data_classes)
CITED = re.compile(r"\[([a-z]+-[0-9a-f]+)\]")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return build_db.build(DATA / "sample_csv", tmp_path_factory.mktemp("db") / "clinic.db")


def test_the_config_names_sections_fields_and_the_office_lists():
    assert CFG.source == "db" and [s.record.name for s in CFG.sections] == [
        "problems",
        "medications",
        "allergies",
        "vitals",
        "visits",
    ]
    assert CFG.fields["vitals"]["value"] == "value" and "warfarin" in CFG.medication_reason
    assert CFG.vitals["systolic blood pressure"]["max"] == 140 and CFG.interactions
    assert P.tools["chart_brief"].answer == "direct" and P.tools["chart_brief"].when is not None


def test_every_line_of_the_brief_cites_a_record_that_was_returned(db):
    out = cb.brief(db, P, CFG, "Give me a brief on Harold Brinkley", CLINICIAN)
    ids = {r["id"] for r in out["results"]}
    body = out["answer"]
    for line in body.splitlines():
        if (
            line.startswith("- ")
            and "none recorded" not in line
            and not line.startswith("- Every")
            and not line.startswith("- Medication review")
        ):
            cited = CITED.findall(line)
            assert cited, line  # every record line and every check cites
            assert set(cited) <= ids, (line, ids)
    assert all(P.source_id_pattern.fullmatch(i) for i in ids), ids  # the grounding check knows them all


def test_harold_untreated_hypertension_and_high_blood_pressure_are_flagged(db):
    a = cb.brief(db, P, CFG, "chart summary for Harold Brinkley", CLINICIAN)["answer"]
    assert "Essential hypertension [cond-" in a and "none of the medications the office lists for hypertension" in a
    assert "Systolic blood pressure 162 mmHg on 2026-10-30 [vit-" in a and "above the office threshold (140)" in a
    assert "— above the office threshold (140)" in a  # flagged on the vitals line too
    assert "Warfarin" in a and "Reason recorded: Atrial fibrillation" in a
    assert "No reason recorded for Warfarin" not in a  # documented: no flag
    assert "Allergy to Penicillin" in a and "Rash (moderate)" in a
    assert "Medication review: the FDA labels on file mention" in a and "lbl-" in a
    assert "Nothing here is a recommendation" in a


def test_doris_omeprazole_without_a_reason_and_afib_without_an_anticoagulant(db):
    a = cb.brief(db, P, CFG, "brief me on Doris Whitfield", CLINICIAN)["answer"]
    assert "No reason recorded for Omeprazole" in a and "reflux, gerd" in a
    assert "Atrial fibrillation [cond-" in a and "warfarin, apixaban" in a
    assert "No reason recorded for Metformin" not in a and "Allergies:\n- none recorded" in a
    assert "above the office threshold" not in a  # her vitals are in range


def test_lena_temperature_and_nothing_else(db):
    a = cb.brief(db, P, CFG, "What do we know about Lena Okafor", CLINICIAN)["answer"]
    assert "Body temperature 38.4 Cel on 2026-10-31 [vit-" in a and "above the office threshold (38.0)" in a
    assert "No reason recorded" not in a and "is on the problem list, and none" not in a
    assert "no pair of these medications is mentioned" in a


def test_roles_a_nurse_gets_the_brief_the_front_desk_does_not(db):
    nurse = cb.brief(db, P, CFG, "brief on Harold Brinkley", NURSE)
    assert "Problem list:" in nurse["answer"] and "Latest vitals:" in nurse["answer"]
    assert not any(r["id"].startswith("clm-") for r in nurse["results"])
    front = cb.brief(db, P, CFG, "brief on Harold Brinkley", FRONT)
    assert front["results"] == [] and "this role may not see" in front["answer"]
    assert "chart_brief" not in P.roles["front_desk"].tools  # and the planner never calls it for them


def test_unknown_or_ambiguous_patient(db):
    assert "couldn't find" in cb.brief(db, P, CFG, "brief on Nobody Here", CLINICIAN)["answer"]
    assert cb.brief(None, P, CFG, "x", CLINICIAN)["note"].startswith("chart brief is not set up")


def test_the_latest_of_each_vital_sign_is_used():
    rows = [
        {"kind": "Heart rate", "value": 70, "date": "2026-01-01", "_id": "vit-1"},
        {"kind": "Heart rate", "value": 120, "date": "2026-02-01", "_id": "vit-2"},
        {"kind": "heart rate", "value": 65, "date": "2025-12-01", "_id": "vit-3"},
    ]
    latest = cb.latest_vitals(rows)
    assert [v["_id"] for v in latest] == ["vit-2"]
    lines, flags = cb.check_vitals(latest, CFG)
    assert flags == {("vit-2", "heart rate"): "above the office threshold (100)"} and "120" in lines[0]


def test_the_trigger_and_the_exemption():
    from agents.tool_choice import tools_to_call
    from guardrails.rules import check_input

    for q in ("Give me a brief on Harold Brinkley", "Chart summary for Harold Brinkley", "What do we know about Harold Brinkley"):
        assert tools_to_call(P, "planner", q)[1] == ["chart_brief"], q
        assert check_input(q, P).allowed
    assert tools_to_call(P, "planner", "What medications is Harold Brinkley taking?")[1] == []
    assert tools_to_call(P, "planner", "Check Harold Brinkley's medications for interactions")[1] == ["medication_review"]


# --- FHIR (clinic-epic), with a fake Epic -------------------------------------------------------


class FakeEpic:
    """Enough of Epic's sandbox for the brief: a patient, and resources by type."""

    def __init__(self, resources: dict[str, list[dict]]):
        self.resources, self.searched = resources, []

    def search(self, resource, params, limit):
        self.searched.append((resource, dict(params)))
        if resource == "Patient":
            return [
                {
                    "id": "eXYZ12345678",
                    "name": [{"given": ["Camila"], "family": "Lopez"}],
                    "birthDate": "1987-09-12",
                    "gender": "female",
                    "address": [{"city": "Madison"}],
                }
            ]
        return self.resources.get(resource, [])[:limit]

    def read(self, resource, rid):
        raise AssertionError("not needed")


EPIC = FakeEpic(
    {
        "Condition": [
            {"id": "cAAAAAAA1111", "code": {"text": "Atrial fibrillation"}, "onsetDateTime": "2024-03-01T00:00:00Z"},
            {"id": "cBBBBBBB2222", "code": {"coding": [{"display": "Essential hypertension"}]}, "recordedDate": "2022-01-05"},
        ],
        "MedicationRequest": [
            {
                "id": "mAAAAAAA1111",
                "medicationCodeableConcept": {"text": "warfarin 5 mg tablet"},
                "authoredOn": "2024-03-02",
                "reasonReference": [{"display": "Atrial fibrillation"}],
            },
            {"id": "mBBBBBBB2222", "medicationReference": {"display": "omeprazole 20 mg capsule"}, "authoredOn": "2025-01-10"},
        ],
        "AllergyIntolerance": [
            {"id": "aAAAAAAA1111", "code": {"text": "Penicillin G"}, "reaction": [{"manifestation": [{"text": "Hives"}]}]}
        ],
        "Observation": [
            {
                "id": "oAAAAAAA1111",
                "code": {"text": "Blood pressure"},
                "effectiveDateTime": "2026-10-20T10:00:00Z",
                "component": [
                    {"code": {"text": "Systolic blood pressure"}, "valueQuantity": {"value": 158, "unit": "mmHg"}},
                    {"code": {"text": "Diastolic blood pressure"}, "valueQuantity": {"value": 94, "unit": "mmHg"}},
                ],
            },
            {
                "id": "oBBBBBBB2222",
                "code": {"text": "Heart rate"},
                "effectiveDateTime": "2026-10-20T10:00:00Z",
                "valueQuantity": {"value": 76, "unit": "/min"},
            },
        ],
        "Encounter": [],
    }
)


def test_the_brief_from_a_fhir_server_reads_only_allowed_resources_and_splits_blood_pressure():
    E = load_profile("clinic-epic")
    cfg = cb.load_config(E)
    assert cfg.source == "fhir"
    epic = FakeEpic(EPIC.resources)
    out = cb.brief(
        None, E, cfg, "Give me a brief on Camila Lopez, born 1987-09-12", list(E.roles["clinician"].data_classes), epic
    )
    a = out["answer"]
    assert {r for r, _ in epic.searched} == {
        "Patient",
        "Condition",
        "MedicationRequest",
        "AllergyIntolerance",
        "Observation",
        "Encounter",
    }
    from datetime import date, timedelta
    two_years = "ge" + (date.today() - timedelta(days=730)).isoformat() + "T00:00:00Z"
    assert ("Observation", {"patient": "eXYZ12345678", "category": "vital-signs", "_count": 250,
                            "date": two_years}) in epic.searched    # the last 2 years, newest found by date
    assert "Chart brief for Camila Lopez (born 1987-09-12" in a
    assert "Atrial fibrillation, on the problem list since 2024-03-01 [cond-AAAA1111]" in a
    assert "Essential hypertension, on the problem list since 2022-01-05 [cond-BBBB2222]" in a
    assert "Systolic blood pressure 158 mmHg, measured 2026-10-20 [vit-AAAA1111] — above the office threshold (140)" in a
    assert "Diastolic blood pressure 94 mmHg, measured 2026-10-20 [vit-AAAA1111] — above the office threshold (90)" in a
    assert "Heart rate 76 /min, measured 2026-10-20 [vit-BBBB2222]" in a
    assert "Allergy to Penicillin G. Recorded reaction: Hives [alg-AAAA1111]" in a
    assert "No reason recorded for Omeprazole 20 mg capsule [med-BBBB2222]" in a
    assert "No reason recorded for warfarin" not in a  # reasonReference counts as a reason
    assert "Warfarin 5 mg tablet, ordered 2024-03-02. Reason recorded: Atrial fibrillation [med-AAAA1111]" in a
    assert "Essential hypertension [cond-BBBB2222] is on the problem list, and none of the medications" in a
    ids = {r["id"] for r in out["results"]}
    assert all(E.source_id_pattern.fullmatch(i) for i in ids), ids
    # the front desk: nothing clinical is requested from Epic at all
    epic2 = FakeEpic(EPIC.resources)
    out = cb.brief(None, E, cfg, "brief on Camila Lopez, born 1987-09-12", list(E.roles["front_desk"].data_classes), epic2)
    assert "this role may not see" in out["answer"] and epic2.searched == []


def test_without_a_date_of_birth_nothing_is_sent_to_epic():
    E = load_profile("clinic-epic")
    epic = FakeEpic(EPIC.resources)
    out = cb.brief(None, E, cb.load_config(E), "Give me a brief on Camila Lopez", list(E.roles["clinician"].data_classes), epic)
    assert "date of birth" in out["answer"] and epic.searched == []


def test_one_patient_reader_serves_both_sources(db):
    """mcp_servers/common/patients.py: the same calls on the database and on a FHIR server."""
    from mcp_servers.common.patients import NotFound, Source

    src = Source.open(P, "db", db)
    found = src.find("Harold Brinkley")
    assert found.name == "Harold Brinkley" and found.cited_id == "pt-0000a001" and "born 1948-03-14" in found.text
    assert isinstance(src.find("Nobody"), NotFound) and src.find("Nobody").why == "none"
    rows = src.rows(src.record_type("medications"), found, {"name": "description", "reason": "reason"})
    assert {r["name"][:8] for r in rows} == {"Ciproflo", "Ibuprofe", "Warfarin"} and all(r["_id"].startswith("med-") for r in rows)
    E = load_profile("clinic-epic")
    fsrc = Source.open(E, "fhir", client=FakeEpic(EPIC.resources))
    found = fsrc.find("Camila Lopez, born 1987-09-12")
    assert found.name == "Camila Lopez" and found.cited_id == "pt-12345678"
    assert fsrc.find("Camila Lopez").why == "birthdate"
    rows = fsrc.rows(fsrc.record_type("problems"), found, {"name": "code.text,code.coding.display"})
    assert [r["name"] for r in rows] == ["Atrial fibrillation", "Essential hypertension"]
    assert Source.open(P, "fhir") is None and Source.open(E, "db") is None       # a source the profile lacks
