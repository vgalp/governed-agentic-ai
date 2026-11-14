"""Sensitive categories, enforced (ADR 038, step 2): the profile settings, the gateway withholding records
by role, the record tools sending what a resource's codes and labels show, the router keeping a category
on the local model, and what the person is told. Invented data only; OPA, the tools and the models are
fakes (the policies themselves are tested with `opa test policies/`).
"""

import shutil

import pytest
import yaml

import gateway.gateway as gateway
from agents.pipeline import Runtime, checks, handle, stages
from agents.pipeline.runtime import sensitive_reader
from guardrails.rules import GuardrailDecision
from mcp_servers.common.patients import Patient, Source
from mcp_servers.fhir_records import server as fhir_records
from mcp_servers.medication_review import server as medication_review
from privacy.sensitivity import resource_tagger
from profiles.loader import ProfileError, load_profile, load_profile_from
from router import router
from router.router import RouteDecision

EPIC = load_profile("clinic-epic")
CLINIC = load_profile("clinic-assistant")
ALL = ("substance_use", "mental_health", "hiv", "sexual_health", "reproductive_health", "violence", "genetic",
       "protected")
ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
ICD = "http://hl7.org/fhir/sid/icd-10-cm"


# --- the profile ------------------------------------------------------------------------------------------

def test_the_clinic_tracks_every_category_and_keeps_all_of_them_local():
    assert EPIC.sensitivity.categories == ALL == EPIC.sensitivity.never_external
    assert EPIC.roles["clinician"].sensitive_categories == ALL == EPIC.roles["nurse"].sensitive_categories
    assert EPIC.roles["front_desk"].sensitive_categories == () == EPIC.roles["compliance"].sensitive_categories
    data = EPIC.policy_data()
    assert data["roles"]["front_desk"]["sensitive_categories"] == []
    assert data["roles"]["nurse"]["sensitive_categories"] == list(ALL)
    assert data["routing"]["never_external_categories"] == list(ALL)


def test_a_profile_that_tracks_nothing_is_unchanged():
    p = load_profile("general-assistant")
    assert p.sensitivity is None and p.policy_data()["routing"]["never_external_categories"] == []
    assert sensitive_reader(p) is None and resource_tagger(p.sensitivity) is None


@pytest.fixture
def copy_of_epic(tmp_path):
    dst = tmp_path / "clinic-epic"
    shutil.copytree(EPIC.path, dst)
    return dst


def _edit(path, change):
    d = yaml.safe_load(path.read_text())
    change(d)
    path.write_text(yaml.safe_dump(d, sort_keys=False))


def test_settings_are_checked_against_the_value_sets_and_each_other(copy_of_epic):
    profile, roles, responses = (copy_of_epic / f for f in ("profile.yaml", "roles.yaml", "responses.yaml"))
    original = {p: p.read_text() for p in (profile, roles, responses)}

    def broken(path, change, message):
        _edit(path, change)
        with pytest.raises(ProfileError, match=message):
            load_profile_from(copy_of_epic)
        path.write_text(original[path])

    broken(profile, lambda d: d["sensitivity"]["categories"].append("astrology"), "unknown categories astrology")
    broken(profile, lambda d: d["sensitivity"].update(categories=["hiv"], never_external=["genetic"]),
           "never_external names genetic")
    broken(profile, lambda d: d["sensitivity"].update(categories=["hiv"]),
           "role 'clinician': sensitive_categories names substance_use")
    broken(profile, lambda d: d.pop("sensitivity"), "role 'clinician' lists sensitive_categories, but the profile has no")
    broken(profile, lambda d: d["sensitivity"].update(value_sets="../../../etc/passwd"), "outside the profile folder")
    broken(responses, lambda d: d.pop("records_withheld"), "no response for records_withheld")
    assert load_profile_from(copy_of_epic).sensitivity is not None        # and the untouched copy still loads


def test_an_office_can_bring_its_own_lists_and_let_a_category_out(copy_of_epic):
    (copy_of_epic / "our_sets.yaml").write_text("categories: {gout: {title: Gout, drugs: [allopurinol]}}")
    _edit(copy_of_epic / "profile.yaml",
          lambda d: d.update(sensitivity={"categories": ["gout"], "never_external": [], "value_sets": "our_sets.yaml"}))
    _edit(copy_of_epic / "roles.yaml",
          lambda d: [r.pop("sensitive_categories", None) for r in d["roles"].values()])
    p = load_profile_from(copy_of_epic)
    assert p.sensitivity.categories == ("gout",) and p.sensitivity.never_external == ()
    assert sensitive_reader(p)("on allopurinol and methadone") == ("gout",)


# --- the gateway --------------------------------------------------------------------------------------------

def record(rid, text, **extra):
    return {"id": rid, "title": "Current medication", "text": text, "data_classes": ["medications"], **extra}


def test_records_are_marked_from_their_text_and_from_what_the_tool_found():
    results = [record("med-1", "Ann Lee: methadone 10 mg"),
               record("med-2", "Ann Lee: lisinopril 10 mg"),
               record("med-3", "Ann Lee: tablet", sensitivity=["hiv", "astrology"]),   # coded by the tool; one unknown
               record("med-4", "Ann Lee: Biktarvy", sensitivity=[])]                    # a tool cannot remove one
    tagged, counts = gateway.tag_sensitive(results, EPIC)
    assert [r["sensitivity"] for r in tagged] == [["substance_use"], [], ["hiv"], ["hiv"]]
    assert counts == {"substance_use": 1, "hiv": 2} and "sensitivity" not in results[0]     # the input is not changed


def test_a_role_keeps_only_records_whose_categories_it_may_all_see():
    tagged = [{"id": "a", "sensitivity": []}, {"id": "b", "sensitivity": ["hiv"]},
              {"id": "c", "sensitivity": ["hiv", "mental_health"]}]
    kept, withheld = gateway.filter_sensitive(tagged, ["hiv"])
    assert [r["id"] for r in kept] == ["a", "b"] and withheld == {"mental_health": 1}
    kept, withheld = gateway.filter_sensitive(tagged, [])
    assert [r["id"] for r in kept] == ["a"] and withheld == {"hiv": 2, "mental_health": 1}


@pytest.fixture
def gate(monkeypatch):
    """call_tool with OPA, the tool and the audit replaced: {role: categories it may see} decides."""
    seen = {"events": [], "asked": []}

    def use(tool_result, may_see):
        monkeypatch.setattr(gateway, "_is_allowed", lambda *a, **k: True)
        monkeypatch.setattr(gateway, "allowed_classes", lambda p, r: ["medications", "visit_dates"])
        monkeypatch.setattr(gateway, "allowed_sensitive", lambda p, r: seen["asked"].append(r) or may_see.get(r, []))
        monkeypatch.setattr(gateway, "_audit", seen["events"].append)

        async def fake_call(url, tool, args, on_progress=None, timeout=None):
            return tool_result
        monkeypatch.setattr(gateway, "_call", fake_call)

        def call(role, tool="medication_list"):
            return gateway.call_tool("planner", tool, {"query": "Ann Lee"}, "r1", EPIC,
                                     audit_args={"query": "[PERSON_1]"}, role=role)
        return call
    use.seen = seen
    return use


MEDS = {"results": [record("med-1", "Ann Lee: methadone 10 mg"), record("med-2", "Ann Lee: lisinopril 10 mg")],
        "answer": "Current medications:\n- methadone 10 mg [med-1]\n- lisinopril 10 mg [med-2]"}


def test_the_gateway_withholds_a_category_and_the_tools_own_answer_with_it(gate):
    call = gate(MEDS, {"clinician": list(ALL), "nurse": ["hiv"]})
    full = call("clinician")
    assert [r["id"] for r in full["results"]] == ["med-1", "med-2"] and "methadone" in full["answer"]
    assert "withheld_sensitive" not in full

    part = call("nurse")
    assert [r["id"] for r in part["results"]] == ["med-2"]
    assert part["withheld_sensitive"] == 1 and part["answer_withheld"] is True and "answer" not in part
    event = gate.seen["events"][-1]
    assert event["sensitive_categories"] == {"substance_use": 1} and event["withheld_sensitive"] == {"substance_use": 1}
    assert event["returned"] == 1 and "methadone" not in str(event)        # categories and counts, never the record


def test_an_unknown_role_sees_no_category_and_policy_is_not_asked_when_nothing_is_marked(gate):
    call = gate(MEDS, {})
    assert [r["id"] for r in call(None)["results"]] == ["med-2"]
    plain = {"results": [record("med-2", "Ann Lee: lisinopril 10 mg")], "answer": "- lisinopril [med-2]"}
    asked = len(gate.seen["asked"])
    out = gate(plain, {})("front_desk")
    assert out["answer"] == "- lisinopril [med-2]" and out["results"][0]["sensitivity"] == []
    assert len(gate.seen["asked"]) == asked and gate.seen["events"][-1]["sensitive_categories"] == {}


def test_pages_from_the_knowledge_base_are_not_treated_as_records(gate):
    page = {"results": [{"id": "kb-cl-001", "title": "HIV testing policy", "text": "How we offer HIV tests."}]}
    out = gate(page, {})("front_desk", tool="search_knowledge")
    assert out["results"] == page["results"] and "sensitive_categories" not in gate.seen["events"][-1]


# --- the record tools: what only the resource shows ---------------------------------------------------------

LABELLED = {"resourceType": "MedicationRequest", "id": "abc12345", "authoredOn": "2026-03-02",
            "medicationCodeableConcept": {"text": "tablet, oral"}, "meta": {"security": [{"system": ACT, "code": "ETH"}]}}
PLAIN = {"resourceType": "MedicationRequest", "id": "def67890", "authoredOn": "2026-03-02",
         "medicationCodeableConcept": {"text": "lisinopril 10 mg tablet"}}


class OneType:
    """A FHIR client that returns the same resources for any search."""

    def __init__(self, resources):
        self.resources = resources

    def search(self, resource, params, limit):
        return list(self.resources)


def medications():
    return next(r for r in EPIC.fhir_map.records if r.name == "medications")


def test_a_record_carries_the_categories_of_its_resources_labels_and_codes():
    tag = resource_tagger(EPIC.sensitivity)
    assert tag(LABELLED) == ["substance_use"] and tag(PLAIN) == []
    assert tag({"resourceType": "Condition", "code": {"coding": [{"system": ICD, "code": "B20"}]}}) == ["hiv"]
    found = fhir_records.records_of_type(OneType([LABELLED, PLAIN]), medications(), "p1", "Ann Lee", {"medications"}, tag)
    assert [r["sensitivity"] for r in found] == [["substance_use"], []]
    assert "methadone" not in found[0]["text"]                  # the text alone would not have shown it
    untagged = fhir_records.records_of_type(OneType([LABELLED]), medications(), "p1", "Ann Lee", {"medications"})
    assert "sensitivity" not in untagged[0]                     # a profile that tracks nothing: records unchanged


def test_the_chart_and_medication_tools_send_them_too():
    patient = Patient("p1", "Ann Lee", "pt-000000p1", "Ann Lee")
    src = Source("fhir", EPIC, client=OneType([LABELLED, PLAIN]))
    assert [r["sensitivity"] for r in src.records(medications(), patient, {"medications"})] == [["substance_use"], []]
    cfg = medication_review.load_config(EPIC)
    meds = medication_review.medications_of(Source("fhir", EPIC, client=OneType([LABELLED, PLAIN])), cfg, patient)
    assert [m["sensitivity"] for m in meds] == [["substance_use"], []]


# --- the router ---------------------------------------------------------------------------------------------

@pytest.fixture
def open_clinic(copy_of_epic, monkeypatch):
    """The clinic, allowed to send masked text out, so only the category rule keeps a request in."""
    _edit(copy_of_epic / "profile.yaml", lambda d: d["routing"].update(send_external_when="masked"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return load_profile_from(copy_of_epic)


def policy_says(monkeypatch, result):
    seen = {}
    monkeypatch.setattr(router, "decide", lambda profile, path, input_: seen.update(input=input_) or result)
    return seen


def test_the_policy_is_told_the_categories_and_code_holds_whatever_it_answers(monkeypatch, open_clinic):
    seen = policy_says(monkeypatch, {"target": "external", "reasons": ["policy let it out"]})
    out = router.decide_route(open_clinic, [], [], sensitive=("substance_use",))
    assert seen["input"]["sensitive_categories"] == ["substance_use"]
    assert out.target == "local" and "overridden: is about substance_use" in out.reasons[-1]
    assert router.decide_route(open_clinic, [], [], sensitive=()).target == "external"
    assert seen["input"]["sensitive_categories"] == []


def test_the_routing_audit_names_categories_only(monkeypatch, open_clinic):
    policy_says(monkeypatch, {"target": "local", "reasons": ["is about hiv, which never leaves this machine"]})
    events = []

    class Chain:
        def emit(self, topic, event, flush=False):
            events.append((topic, event))
    monkeypatch.setattr(router, "get_audit_chain", lambda agent: Chain())
    router.route(open_clinic, {}, [], "r1", "planner", "nurse", sensitive=("hiv",))
    topic, event = events[-1]
    assert topic == "audit.routing" and event["sensitive_categories"] == ["hiv"] and event["target"] == "local"


# --- the pipeline: what the person is told ----------------------------------------------------------------------

class FakeProducer:
    def __init__(self):
        self.messages = []

    def produce(self, topic, key=None, value=None):
        self.messages.append(topic)

    def flush(self):
        pass

    def poll(self, timeout=0):
        pass


class FakeAudit:
    def __init__(self, producer):
        self.events, self.producer = [], producer

    def emit(self, topic, event, flush=False):
        self.events.append((topic, event))
        return event


def runtime(monkeypatch, tool_results, answer="An answer [vis-00000001]."):
    from guardrails import faithfulness

    producer = FakeProducer()
    calls = {"chat": [], "routes": []}

    def chat(model, messages, purpose="answer"):
        calls["chat"].append(messages)
        return answer

    def call_tool(agent, tool, args, request_id, profile, audit_args=None, role=None, on_progress=None, **kw):
        return tool_results.get(tool, {"results": []})

    def route(P, mapping, used, rid, agent, role, sensitive=()):
        calls["routes"].append(sensitive)
        local = P.models[P.routing["local_model"]]
        return RouteDecision("local", P.routing["local_model"], local, False, ["test"])

    rt = Runtime(EPIC, producer, FakeAudit(producer), chat, call_tool, lambda text, profile: (text, {}),
                 lambda profile, path, inp: {"allowed": True}, "planner",
                 {"question": "guard", "answer": "llama-guard3:8b", "judge": None, "input_agent": None},
                 faithfulness.load_config(EPIC), None, sensitive_reader(EPIC))
    rt.calls = calls
    monkeypatch.setattr(stages, "input_check", lambda text, profile=None: GuardrailDecision(True))
    monkeypatch.setattr(stages, "crisis_over_rule", lambda *a, **k: None)
    monkeypatch.setattr(checks, "classifier_check", lambda q, a, profile=None: GuardrailDecision(True))
    monkeypatch.setattr(checks.toxicity, "score", lambda text: None)
    monkeypatch.setattr(stages, "route", route)
    monkeypatch.setattr(stages, "audit_route", lambda *a, **k: None)
    monkeypatch.setattr(stages, "fallbacks", lambda P, d: [])
    return rt


VISIT = {"id": "vis-00000001", "title": "Visit", "text": "Ann Lee: office visit 2026-03-02.",
         "data_classes": ["visit_dates"], "sensitivity": []}


def test_an_answer_from_part_of_the_record_says_so_without_naming_the_category(monkeypatch):
    rt = runtime(monkeypatch, {"search_records": {"results": [VISIT], "withheld_sensitive": 2}})
    out = handle(rt, {"request_id": "s1", "message": "When was Ann Lee last seen?", "role": "front_desk"})
    assert out.guardrail is None and out.answer.startswith("An answer [vis-00000001].")
    assert out.answer.endswith(EPIC.response("records_not_shown"))
    for word in ("substance", "mental", "HIV", "psychiatr"):
        assert word not in out.answer
    system = rt.calls["chat"][0][0]["content"]
    assert "are not shown for the asker's role" in system and "Do not guess what they contain" in system


def test_a_tools_own_answer_is_not_shown_in_part(monkeypatch):
    rt = runtime(monkeypatch, {"medication_list": {"results": [VISIT], "withheld_sensitive": 1, "answer_withheld": True}})
    out = handle(rt, {"request_id": "s2", "message": "What medications is Ann Lee taking?", "role": "nurse"})
    assert out.answer == EPIC.response("records_withheld")
    assert out.guardrail == {"stage": "access", "layer": "sensitivity", "category": "records_withheld"}
    assert rt.calls["chat"] == []                                   # no model wrote around the gap


def test_every_record_withheld_is_not_reported_as_nothing_found(monkeypatch):
    rt = runtime(monkeypatch, {"medication_list": {"results": [], "withheld_sensitive": 3, "answer_withheld": True}})
    out = handle(rt, {"request_id": "s3", "message": "What medications is Ann Lee taking?", "role": "nurse"})
    assert out.answer == EPIC.response("records_withheld") and rt.calls["chat"] == []


def test_routing_is_given_the_categories_of_the_question_and_of_the_notes(monkeypatch):
    marked = {**VISIT, "sensitivity": ["mental_health"]}
    rt = runtime(monkeypatch, {"search_records": {"results": [marked]}})
    handle(rt, {"request_id": "s4", "message": "Was Ann Lee's last visit about her HIV?", "role": "clinician"})
    assert rt.calls["routes"] == [("hiv", "mental_health")]
    rt = runtime(monkeypatch, {"search_records": {"results": [VISIT]}})
    handle(rt, {"request_id": "s5", "message": "When was Ann Lee last seen?", "role": "clinician"})
    assert rt.calls["routes"] == [()]


# --- the demo patient: something to show (clinic-assistant, database records) ------------------------------

@pytest.fixture(scope="module")
def clinic_db(tmp_path_factory):
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_db", CLINIC.path / "data" / "build_db.py")
    build_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_db)
    path = tmp_path_factory.mktemp("clinic") / "clinic.db"
    build_db.build(CLINIC.path / "data" / "sample_csv", path)
    return build_db, path


def seen_by(role, db_path, name="Maya Castellan"):
    from mcp_servers.records import server as records
    found = records.search(db_path, f"When is {name}'s next appointment?", list(CLINIC.roles[role].data_classes),
                           CLINIC.records_map)
    tagged, marked = gateway.tag_sensitive(found["results"], CLINIC)
    kept, withheld = gateway.filter_sensitive(tagged, list(CLINIC.roles[role].sensitive_categories))
    return kept, dict(marked), dict(withheld)


def test_the_front_desk_is_not_shown_the_demo_patients_psychiatry_appointment(clinic_db):
    _, db_path = clinic_db
    kept, marked, withheld = seen_by("front_desk", db_path)
    assert withheld == {"mental_health": 1} and marked == {"mental_health": 1}
    assert [r["title"] for r in kept] == ["Patient: Maya Castellan"]            # she exists; the visit is not shown
    kept, marked, withheld = seen_by("clinician", db_path)
    assert withheld == {} and marked["mental_health"] >= 2 and marked["substance_use"] >= 2
    assert any("Psychiatry follow-up" in r["text"] for r in kept)
    assert seen_by("front_desk", db_path, "Harold Brinkley")[2] == {}           # an ordinary patient: nothing withheld


def test_a_demo_patient_added_to_the_file_reaches_a_database_built_before_it(clinic_db, tmp_path):
    import sqlite3
    build_db, built = clinic_db
    db_path = tmp_path / "older.db"
    shutil.copy(built, db_path)
    db = sqlite3.connect(db_path)
    for table, column in (("medications", "patient_id"), ("appointments", "patient_id"), ("conditions", "patient_id"),
                          ("allergies", "patient_id"), ("vitals", "patient_id"), ("patients", "id")):
        db.execute(f"DELETE FROM {table} WHERE {column} LIKE '%a004'")        # as built before Maya was written
    db.commit()

    def counts():
        c = sqlite3.connect(db_path)
        return [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("patients", "medications", "appointments", "conditions", "vitals")]
    before = counts()
    build_db.add_chart(db_path)                                                # what dev.sh runs on every start
    after = counts()
    assert [a - b for a, b in zip(after, before)] == [1, 3, 1, 3, 4]
    build_db.add_chart(db_path)
    build_db.add_demo(db_path)
    assert counts() == after                                                   # run again: no change
