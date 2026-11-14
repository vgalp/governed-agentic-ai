"""Sensitive categories of health information: the lists, the detector and its scoring (ADR 038)."""

import json

import pytest

from eval import run_sensitivity
from privacy import sensitivity
from privacy.sensitivity import Detector, Finding, ValueSetError

ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CONF = "http://terminology.hl7.org/CodeSystem/v3-Confidentiality"
ICD = "http://hl7.org/fhir/sid/icd-10-cm"


@pytest.fixture(scope="module")
def detector():
    return Detector(sensitivity.load())


def cats(found):
    return sensitivity.categories(found)


# --- the value sets file ---------------------------------------------------------------------------

def test_the_shipped_value_sets_load_and_name_every_category():
    sets = sensitivity.load()
    assert {"substance_use", "mental_health", "hiv", "sexual_health", "reproductive_health", "violence",
            "genetic", "protected"} == set(sets.categories)
    assert len(sets.sha256) == 64 and sets.labels["BH"] == ("substance_use", "mental_health")


@pytest.mark.parametrize("raw, message", [
    ({}, "needs a 'categories' mapping"),
    ({"categories": {"Bad Name": {"title": "x"}}}, "lower-case"),
    ({"categories": {"a": {}}}, "needs a title"),
    ({"categories": {"a": {"title": "A", "icd": ["F10"]}}}, "unknown setting(s) icd"),
    ({"categories": {"a": {"title": "A", "drugs": "methadone"}}}, "must be a list"),
    ({"categories": {"a": {"title": "A", "phrases": ["(unclosed"]}}}, "not a valid regular expression"),
])
def test_a_broken_value_sets_file_is_refused_with_the_reason(raw, message):
    with pytest.raises(ValueSetError, match=message.replace("(", r"\(").replace(")", r"\)")):
        sensitivity.parse(raw)


def test_the_file_is_read_again_when_it_changes(tmp_path):
    path = tmp_path / "sets.yaml"
    path.write_text("categories: {a: {title: A, drugs: [alpha]}}")
    assert cats(Detector(sensitivity.load(path)).of_text("on alpha")) == ("a",)
    path.write_text("categories: {a: {title: A, drugs: [beta]}}")
    import os
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert cats(Detector(sensitivity.load(path)).of_text("on alpha")) == ()
    with pytest.raises(ValueSetError, match="cannot read"):
        sensitivity.load(tmp_path / "missing.yaml")


# --- text --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Is she still on Methadone 80 mg?", ("substance_use",)),
    ("Problem list: opioid use disorder.", ("substance_use",)),
    ("Diagnosed with bipolar disorder in 2021.", ("mental_health",)),
    ("Tomás is HIV positive and takes Biktarvy.", ("hiv",)),
    ("Treated for chlamydia; history of sexual assault.", ("sexual_health", "violence")),
    ("IUD placed 2024.", ("reproductive_health",)),
    ("BRCA1 positive.", ("genetic",)),
    ("Blood pressure 128/82.", ()),
    ("", ()),
])
def test_text_is_sorted_into_its_categories(detector, text, expected):
    assert cats(detector.of_text(text)) == expected


@pytest.mark.parametrize("text", [
    "ECG shows 2 mm ST depression.",
    "Monitor for respiratory depression.",
    "New batteries for her hearing aids.",       # not AIDS
    "Bowel prep instructions given.",            # not PrEP
    "Anorexia since starting chemotherapy.",     # the symptom, not anorexia nervosa
    "Tenofovir for chronic hepatitis B.",
    "Naloxone kit offered with her oxycodone.",
    "A methadoneX trial",                        # whole words only
])
def test_wording_that_usually_means_something_else_is_not_marked(detector, text):
    assert detector.of_text(text) == []


def test_a_finding_names_the_list_entry_never_the_records_words(detector):
    found = detector.of_text("Kenji Morita, 12 Elm Street, was admitted for heroin withdrawal and takes methadone.")
    assert Finding("substance_use", "medication", "methadone") in found
    assert all("Kenji" not in f.evidence and "Elm" not in f.evidence for f in found)
    assert sensitivity.summary(found) == {"substance_use": ["medication", "text"]}


# --- FHIR resources -----------------------------------------------------------------------------------

def condition(system, code, display="Condition", **extra):
    return {"resourceType": "Condition", "code": {"coding": [{"system": system, "code": code, "display": display}]},
            **extra}


@pytest.mark.parametrize("system, code, expected", [
    (ICD, "F11.20", ("substance_use",)),
    (ICD, "f1120", ("substance_use",)),                               # with or without the dot, any case
    ("urn:oid:2.16.840.1.113883.6.90", "F10.239", ("substance_use",)),
    (ICD, "F17.210", ()),                                             # nicotine is left out
    (ICD, "B20", ("hiv",)),
    (ICD, "T74.11XA", ("violence",)),
    (ICD, "Z3A.20", ()),                                              # pregnancy in general is left out
    ("http://snomed.info/sct", "F11.20", ()),                         # an ICD-looking code in another system
])
def test_icd10_codes_are_matched_by_prefix_in_icd10_systems_only(detector, system, code, expected):
    assert cats(detector.of_resource(condition(system, code))) == expected


def test_a_source_systems_label_is_trusted_and_only_where_labels_live(detector):
    labelled = {"resourceType": "Observation", "meta": {"security": [{"system": ACT, "code": "ETH"}]},
                "code": {"text": "Urine screen"}}
    assert detector.of_resource(labelled) == [Finding("substance_use", "label", "hl7:ETH")]
    restricted = {"resourceType": "Encounter", "meta": {"security": [{"system": CONF, "code": "R"}]}}
    assert detector.of_resource(restricted) == [Finding("protected", "label", "confidentiality:R")]
    normal = {"resourceType": "Encounter", "meta": {"security": [{"system": CONF, "code": "N"}]}}
    assert detector.of_resource(normal) == []
    # the same code outside meta.security is not a label
    elsewhere = {"resourceType": "Observation", "code": {"coding": [{"system": ACT, "code": "ETH"}]}}
    assert detector.of_resource(elsewhere) == []
    document = {"resourceType": "DocumentReference", "securityLabel": [{"coding": [{"system": ACT, "code": "HIV"}]}]}
    assert cats(detector.of_resource(document)) == ("hiv",)


def test_a_resources_medication_and_text_are_read(detector):
    med = {"resourceType": "MedicationRequest", "medicationReference": {"reference": "Medication/1",
                                                                         "display": "Biktarvy 50-200-25 mg tablet"}}
    assert detector.of_resource(med) == [Finding("hiv", "medication", "biktarvy")]
    note = {"resourceType": "Condition", "text": {"status": "generated", "div": "<div>History of <b>bipolar</b> disorder</div>"},
            "note": [{"text": "Patient disclosed domestic violence."}]}
    assert cats(detector.of_resource(note)) == ("mental_health", "violence")
    with pytest.raises(TypeError):
        detector.of_resource(["not", "a", "resource"])


# --- a text model ---------------------------------------------------------------------------------------

def test_a_model_adds_what_the_lists_cannot_know_above_its_threshold():
    sets = sensitivity.load()
    implied = "He finishes a fifth of vodka most nights."
    assert Detector(sets).of_text(implied) == []
    model = Detector(sets, model=lambda text: {"substance_use": 0.91, "mental_health": 0.2}, threshold=0.5)
    assert model.of_text(implied) == [Finding("substance_use", "model", "0.91")]
    with pytest.raises(ValueError, match="unknown category"):
        Detector(sets, model=lambda text: {"nonsense": 0.9}).of_text(implied)
    with pytest.raises(ValueError, match="between 0 and 1"):
        Detector(sets, threshold=1.0)


# --- scoring ----------------------------------------------------------------------------------------------

def test_the_development_set_is_well_formed():
    rows = run_sensitivity.read_rows(run_sensitivity.DEV, set(sensitivity.load().categories))
    assert len(rows) == 126 and len({r["id"] for r in rows}) == len(rows)
    assert {r["stratum"] for r in rows} == set(run_sensitivity.POSITIVE + run_sensitivity.NEGATIVE)


def test_scoring_counts_by_stratum_and_refuses_a_row_that_contradicts_itself(tmp_path):
    rows = [{"id": "a", "kind": "text", "stratum": "explicit", "categories": ["hiv"], "text": "HIV positive"},
            {"id": "b", "kind": "text", "stratum": "implicit", "categories": ["hiv"], "text": "his counts are good"},
            {"id": "c", "kind": "text", "stratum": "hard_negative", "categories": [], "text": "hearing aids"},
            {"id": "d", "kind": "text", "stratum": "plain", "categories": [], "text": "on methadone"}]
    path = tmp_path / "set.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    out = run_sensitivity.run(dataset=path)
    s = out["summary"]
    assert s["per_category"]["hiv"]["caught"] == {"explicit": 1, "implicit": 0}
    assert s["per_category"]["substance_use"]["marked_without_it"] == 1
    assert s["negatives"]["plain"] == {"marked": 1, "total": 1} and s["exactly_right"] == 2
    assert "| HIV status, testing and treatment | 1/1 | 0/1 | – | 0/2 |" in run_sensitivity.table(s, out["titles"])
    assert out["predictions"][3]["evidence"] == {"substance_use": ["medication"]}      # kinds, never the text

    path.write_text(json.dumps({**rows[0], "stratum": "plain"}))
    with pytest.raises(ValueError, match="does not fit"):
        run_sensitivity.run(dataset=path)


# --- a language model as the reader (step 3) ----------------------------------------------------------------

def test_the_model_is_told_the_readable_categories_and_the_rules():
    from privacy import sensitivity_model as sm
    sets = sensitivity.load()
    system, user = sm.messages_for("on methadone", sets)
    assert "- substance_use: Problem use of alcohol or drugs" in system["content"]
    assert "- protected:" not in system["content"]                 # a label on a record: nothing to read for
    assert "about no one" in system["content"] and user["content"] == "Text:\non methadone"
    assert set(sm.readable_categories(sets)) == set(sets.categories) - {"protected"}


@pytest.mark.parametrize("reply, expected", [
    ('{"categories": ["hiv", "substance_use"]}', ["hiv", "substance_use"]),
    ('```json\n{"categories": ["hiv"]}\n```', ["hiv"]),
    ('Sure. {"categories": []} Hope that helps.', []),
    ('{"categories": ["hiv", "astrology", "hiv", 7]}', ["hiv"]),    # invented names and repeats are dropped
    ("[]", []),                                                     # how a local model says "none"
    ("{}", []),
    ('["hiv"]', ["hiv"]),
    ('{"labels": ["hiv"]}', None),                                  # another shape: not guessed
    ('{"categories": "hiv"}', None),
    ("I think this is about HIV.", None),
    ("", None),
])
def test_a_models_reply_is_read_leniently_and_never_guessed(reply, expected):
    from privacy import sensitivity_model as sm
    assert sm.parse(reply, {"hiv", "substance_use"}) == expected


def test_a_reader_adds_implied_wording_and_counts_what_it_could_not_read():
    from privacy import sensitivity_model as sm
    sets = sensitivity.load()
    replies = iter(['{"categories": ["substance_use"]}', "no idea", '{"categories": ["mental_health"]}'])
    ticks = iter(range(100))
    reader = sm.LlmReader(lambda messages: next(replies), sets, clock=lambda: next(ticks))
    d = Detector(sets, model=reader)
    assert d.of_text("He finishes a fifth of vodka most nights.") == [Finding("substance_use", "model", "1.00")]
    assert d.of_text("On methadone.") == [Finding("substance_use", "medication", "methadone")]   # unreadable: lists hold
    alone = Detector(sets, model=reader, lists=False)
    assert cats(alone.of_text("On methadone, low mood.")) == ("mental_health",)                  # the model only
    assert (reader.calls, reader.unreadable, reader.seconds) == (3, 1, [1, 1, 1])
    with pytest.raises(ValueError, match="a text model is needed"):
        Detector(sets, lists=False)


def test_the_scorer_measures_a_model_with_or_without_the_lists(tmp_path):
    rows = [{"id": "a", "kind": "text", "stratum": "implicit", "categories": ["hiv"], "text": "his counts are good"},
            {"id": "b", "kind": "text", "stratum": "explicit", "categories": ["hiv"], "text": "HIV positive"}]
    path = tmp_path / "set.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    seen = []

    def model(text):
        return {"hiv": 1.0} if "counts" in text else {}
    both = run_sensitivity.run(dataset=path, model=model, name="lists+m", progress=lambda n, t: seen.append((n, t)))
    assert both["summary"]["exactly_right"] == 2 and both["detector"] == "lists+m" and seen == [(1, 2), (2, 2)]
    alone = run_sensitivity.run(dataset=path, model=model, name="m", lists=False)
    assert alone["summary"]["per_category"]["hiv"]["caught"] == {"implicit": 1, "explicit": 0}
