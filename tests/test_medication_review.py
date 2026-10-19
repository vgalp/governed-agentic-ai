"""Medication review (ADR 017): current medications, and where one FDA label mentions another,
quoted word for word; clinicians only; never a recommendation; no model."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

from agents.tool_choice import tool_denied_reply, tools_to_call
from guardrails.rules import check_input, classifier_exemption, GuardrailDecision
from mcp_servers.medication_review.server import ingredients_of, load_config, quote, review
from profiles.loader import ProfileError, load_profile, load_profile_from

CLINIC = load_profile("clinic-assistant")
DATA = CLINIC.path / "data"
_spec = importlib.util.spec_from_file_location("build_db", DATA / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)

# Invented label text in the shape openFDA returns. NOT real FDA labels: tests only.
FAKE_LABELS = {
    "downloaded": "2026-10-19",
    "medications": {"Warfarin Sodium 5 MG Oral Tablet": {"rxcui": "1", "ingredients": ["warfarin"]}},
    "ingredients": {
        "warfarin": {"set_id": "aaaabbbb-0000-0000-0000-000000000001", "effective_time": "20250301",
                     "title": "Warfarin Sodium", "brand_names": ["Coumadin"], "classes": ["Vitamin K Antagonist"],
                     "sections": {"drug_interactions": [
                         "7.1 General. Test text one. Drugs that increase bleeding risk include NSAIDs and "
                         "antiplatelet agents; monitor INR closely. CYP1A2 inhibitors: ciprofloxacin, test."]}},
        "ibuprofen": {"set_id": "ccccdddd-0000-0000-0000-000000000002", "effective_time": "20240115",
                      "title": "Ibuprofen", "brand_names": [], "classes": ["Nonsteroidal Anti-inflammatory Drug"],
                      "sections": {"drug_interactions": ["Anticoagulants: test text about bleeding."]}},
        "ciprofloxacin": {"set_id": "eeeeffff-0000-0000-0000-000000000003", "effective_time": "20230610",
                          "title": "Ciprofloxacin", "brand_names": ["Cipro"], "classes": ["Fluoroquinolone Antibacterial"],
                          "sections": {"drug_interactions": ["Theophylline: test text."]}},
        "amoxicillin": {"set_id": "99998888-0000-0000-0000-000000000004", "effective_time": "20220101",
                        "title": "Amoxicillin", "brand_names": [], "classes": ["Penicillin-class Antibacterial"],
                        "sections": {"drug_interactions": ["Probenecid: test text."]}},
    },
}


@pytest.fixture
def clinic(tmp_path):
    """The clinic profile with a fresh records database and the fake label file."""
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(CLINIC.path, dst, ignore=shutil.ignore_patterns("clinic.db", "drug_labels.json"))
    build_db.build(DATA / "sample_csv", dst / "data" / "clinic.db")
    (dst / "data" / "drug_labels.json").write_text(json.dumps(FAKE_LABELS))
    p = load_profile_from(dst)
    return p, load_config(p)


ALL = ["patient_details", "medications"]


def test_finds_label_mentions_by_name_and_by_class_and_quotes_them(clinic):
    p, cfg = clinic
    out = review(p.records_db, p, cfg, "Check Harold Brinkley's medications for interactions", ALL)
    a = out["answer"]
    assert "Warfarin Sodium 5 MG Oral Tablet" in a and "Ibuprofen 400 MG Oral Tablet" in a
    assert 'mentions "NSAIDs"' in a                      # ibuprofen found through its FDA class
    assert 'mentions "ciprofloxacin"' in a               # by name
    assert 'mentions "Anticoagulants"' in a              # the ibuprofen label, about warfarin
    assert "lbl-aaaabbbb" in a and "lbl-ccccdddd" in a
    assert "not a recommendation" in a and "does not mean they are safe" in a
    ids = {r["id"] for r in out["results"]}
    assert {"lbl-aaaabbbb", "lbl-ccccdddd"} <= ids and any(i.startswith("med-") for i in ids)
    assert all(r["data_classes"] == ["medications"] for r in out["results"])


def test_every_cited_id_was_returned_so_the_grounding_check_passes(clinic):
    p, cfg = clinic
    out = review(p.records_db, p, cfg, "medication review for Harold Brinkley", ALL)
    cited = {m.group(0) for m in p.source_id_pattern.finditer(out["answer"])}
    assert cited and cited <= {r["id"] for r in out["results"]}


def test_a_pair_the_labels_do_not_mention_is_never_called_safe(clinic):
    p, cfg = clinic
    a = review(p.records_db, p, cfg, "Do Lena Okafor's medications interact?", ALL)["answer"]
    assert "not mentioned in each other's FDA labels" in a and "does not mean they are safe" in a
    assert "levothyroxine" in a.lower()                          # no label on file for it: said plainly


def test_a_role_without_medication_records_gets_nothing(clinic):
    p, cfg = clinic
    out = review(p.records_db, p, cfg, "medication review for Harold Brinkley", ["patient_details"])
    assert out["results"] == [] and "answer" not in out


def test_without_the_label_file_it_says_so(clinic):
    p, _ = clinic
    (p.path / "data" / "drug_labels.json").unlink()
    out = review(p.records_db, p, load_config(p), "medication review for Harold Brinkley", ALL)
    assert "not been downloaded" in out["answer"] and out["results"] == []


def test_unknown_patient(clinic):
    p, cfg = clinic
    assert "couldn't find" in review(p.records_db, p, cfg, "medication review for Nobody Here", ALL)["answer"]


def test_ingredients_and_quotes():
    assert ingredients_of("Warfarin Sodium 5 MG Oral Tablet", FAKE_LABELS) == ["warfarin"]
    assert ingredients_of("Ciprofloxacin 500 MG Oral Tablet", FAKE_LABELS) == ["ciprofloxacin"]
    text = "First sentence. The second one mentions NSAIDs here. Third."
    i = text.index("NSAIDs")
    assert quote(text, i, i + 6) == "The second one mentions NSAIDs here."
    long = "x " * 400 + "NSAIDs" + " y" * 400
    j = long.index("NSAIDs")
    q = quote(long, j, j + 6)
    assert "NSAIDs" in q and len(q) <= 330 and q.startswith("…") and q.endswith("…")


# --- which tools a question gets, and who may use them ------------------------------------------

@pytest.mark.parametrize("q, review_tool", [
    ("Check Harold Brinkley's medications for interactions.", True),
    ("Do Harold Brinkley's medications interact?", True),
    ("Medication review for Doris Whitfield", True),
    ("What medications is Chester Aufderhar taking?", False),
    ("When is Chester Aufderhar's next appointment?", False),
])
def test_the_review_tool_is_called_only_for_review_questions_and_then_alone(q, review_tool):
    to_call, triggered = tools_to_call(CLINIC, "planner", q)
    assert (to_call == ["medication_review"]) is review_tool
    assert ("medication_review" in to_call) is review_tool and bool(triggered) is review_tool


def test_only_clinicians_have_it_and_others_are_told_who_does():
    assert "medication_review" in CLINIC.roles["clinician"].tools
    assert all("medication_review" not in CLINIC.roles[r].tools for r in ("nurse", "front_desk", "compliance"))
    msg = tool_denied_reply(CLINIC, "medication_review", "nurse")
    assert "not available to your role (Nurse)" in msg and "Clinician" in msg


@pytest.mark.parametrize("q", [
    "Should we stop his medications given the interactions?",
    "Can we combine Harold Brinkley's medications?",
    "Is it safe to combine Harold Brinkley's medications?",
    "How much warfarin should Harold Brinkley take with the interactions?",
])
def test_a_decision_is_still_blocked_even_when_it_mentions_interactions(q):
    blocked = not check_input(q, CLINIC).allowed
    flagged = GuardrailDecision(False, "medical_advice", "classifier")
    assert blocked or classifier_exemption(q, flagged, CLINIC) is None


def test_a_review_request_flagged_by_the_classifier_is_let_through_but_never_a_crisis():
    q = "Check Harold Brinkley's medications for interactions."
    assert check_input(q, CLINIC).allowed
    assert classifier_exemption(q, GuardrailDecision(False, "medical_advice", "classifier"), CLINIC)
    assert classifier_exemption(q, GuardrailDecision(False, "crisis", "classifier"), CLINIC) is None


# --- the loader keeps it narrow -----------------------------------------------------------------

def _edit_tools(tmp_path, change):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(CLINIC.path, dst, ignore=shutil.ignore_patterns("clinic.db"))
    t = yaml.safe_load((dst / "tools.yaml").read_text())
    change(t, dst)
    (dst / "tools.yaml").write_text(yaml.safe_dump(t))
    return dst


def test_a_direct_answer_needs_a_when(tmp_path):
    dst = _edit_tools(tmp_path, lambda t, d: t["medication_review"].pop("when"))
    with pytest.raises(ProfileError, match="needs 'when'"):
        load_profile_from(dst)


def test_when_must_name_a_pattern(tmp_path):
    dst = _edit_tools(tmp_path, lambda t, d: t["medication_review"].update(when="no_such_pattern"))
    with pytest.raises(ProfileError, match="not a pattern"):
        load_profile_from(dst)


def test_a_triggered_tool_needs_the_tool_denied_reply(tmp_path):
    def drop(t, d):
        r = yaml.safe_load((d / "responses.yaml").read_text())
        r.pop("tool_denied")
        (d / "responses.yaml").write_text(yaml.safe_dump(r))
    with pytest.raises(ProfileError, match="tool_denied"):
        load_profile_from(_edit_tools(tmp_path, drop))


def test_demo_patients_are_in_the_database_and_marked_synthetic(tmp_path):
    import sqlite3
    db = sqlite3.connect(build_db.build(DATA / "sample_csv", tmp_path / "c.db"))
    names = {n for (n,) in db.execute("SELECT name FROM patients")}
    assert {"Harold Brinkley", "Doris Whitfield", "Lena Okafor"} <= names
    assert "synthetic" in (DATA / "demo_patients.yaml").read_text().lower()
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


# --- the download script (no network: openFDA and RxNorm answers are faked) ---------------------

def _fetch_module():
    spec = importlib.util.spec_from_file_location("fetch_drug_labels", Path("scripts/fetch_drug_labels.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_fetch_picks_the_single_ingredient_label_and_keeps_only_what_is_needed(monkeypatch):
    f = _fetch_module()
    sent = []

    def fake_get(url):
        sent.append(url)
        if "approximateTerm" in url:
            return {"approximateGroup": {"candidate": [{"rxcui": "855332"}]}}
        if "related.json" in url:
            return {"relatedGroup": {"conceptGroup": [{"conceptProperties": [{"name": "Warfarin"}]}]}}
        return {"results": [
            {"set_id": "combo", "effective_time": "20260101", "drug_interactions": ["x"],
             "openfda": {"generic_name": ["WARFARIN AND SOMETHING"]}},
            {"set_id": "old", "effective_time": "20200101", "drug_interactions": ["old text"],
             "openfda": {"generic_name": ["WARFARIN SODIUM"]}},
            {"set_id": "new", "effective_time": "20250301", "drug_interactions": ["new text"],
             "boxed_warning": ["not kept"], "openfda": {"generic_name": ["WARFARIN SODIUM"],
                                                        "brand_name": ["Coumadin"],
                                                        "pharm_class_epc": ["Vitamin K Antagonist [EPC]"]}}]}
    monkeypatch.setattr(f, "get", fake_get)
    assert f.rxnorm("Warfarin Sodium 5 MG Oral Tablet") == ("855332", ["warfarin"])
    lab = f.label("warfarin", ["contraindications", "drug_interactions"])
    assert lab["set_id"] == "new" and lab["classes"] == ["Vitamin K Antagonist"] and lab["brand_names"] == ["Coumadin"]
    assert lab["sections"] == {"drug_interactions": ["new text"]}
    assert "api.fda.gov" in sent[-1] and "HUMAN%20PRESCRIPTION%20DRUG" in sent[-1]


def test_with_no_label_for_any_medication_it_does_not_say_not_mentioned(clinic):
    p, cfg = clinic
    a = review(p.records_db, p, cfg, "medication review for Doris Whitfield", ALL)["answer"]
    assert "nothing could be checked" in a and "not mentioned" not in a


def test_demo_patients_can_be_added_to_an_existing_database_once(tmp_path):
    import sqlite3
    db_path = tmp_path / "c.db"
    build_db.build(DATA / "sample_csv", db_path)
    before = sqlite3.connect(db_path).execute("SELECT COUNT(*), (SELECT COUNT(*) FROM appointments) FROM patients").fetchone()
    build_db.add_demo(db_path)                     # already there: no change
    assert sqlite3.connect(db_path).execute(
        "SELECT COUNT(*), (SELECT COUNT(*) FROM appointments) FROM patients").fetchone() == before
    sqlite3.connect(db_path).executescript(
        "DELETE FROM medications WHERE patient_id LIKE '00000000-%'; DELETE FROM appointments WHERE patient_id LIKE '00000000-%';"
        "DELETE FROM patients WHERE id LIKE '00000000-%';")
    build_db.add_demo(db_path)
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM patients").fetchone()[0] == before[0]


def test_quotes_cut_on_whole_words():
    long = "Drugs listed: " + ", ".join(f"drugname{i}" for i in range(80)) + ", NSAIDs, " + \
           ", ".join(f"other{i}" for i in range(80)) + "."
    j = long.index("NSAIDs")
    q = quote(long, j, j + 6)
    assert "NSAIDs" in q
    words = q.strip("…").split(", ")
    assert all(w.startswith(("drugname", "other", "NSAIDs")) for w in words)


def test_fetch_prefers_the_plain_label_and_skips_only_combinations(monkeypatch):
    f = _fetch_module()
    rows = {"fluoride": [
        {"set_id": "pet", "effective_time": "20260610", "drug_interactions": ["x"],
         "openfda": {"generic_name": ["SODIUM FLUORIDE F-18"]}},
        {"set_id": "plain", "effective_time": "20200101", "drug_interactions": ["y"],
         "openfda": {"generic_name": ["SODIUM FLUORIDE"]}}],
        "b12": [{"set_id": "multi", "effective_time": "20250428", "drug_interactions": ["z"],
                 "openfda": {"generic_name": ["VITAMIN C, VITAMIN D, VITAMIN B12"]}}]}
    monkeypatch.setattr(f, "get", lambda url: {"results": rows["fluoride" if "fluoride" in url else "b12"]})
    assert f.label("sodium fluoride", ["drug_interactions"])["set_id"] == "plain"
    assert f.label("vitamin b12", ["drug_interactions"]) is None
    rows["b12"] = [{"set_id": "combo", "effective_time": "20260902", "drug_interactions": ["s"],
                    "openfda": {"generic_name": ["SULFAMETHOXAZOLE AND TRIMETHOPRIM"]}}]
    assert f.label("sulfamethoxazole", ["drug_interactions"])["set_id"] == "combo"   # only sold combined: kept


def test_the_drug_s_own_name_is_preferred_over_its_class(clinic):
    p, cfg = clinic
    labels = json.loads(json.dumps(FAKE_LABELS))
    labels["ingredients"]["warfarin"]["sections"]["drug_interactions"] = [
        "Nonsteroidal Anti-Inflammatory Agents celecoxib, ibuprofen, naproxen."]
    from mcp_servers.medication_review.server import Config, mentions
    c = Config(labels, cfg.medications, cfg.drug_column, cfg.sections, cfg.class_terms, cfg.data_class)
    assert mentions("warfarin", "ibuprofen", c)[0]["term"] == "ibuprofen"
