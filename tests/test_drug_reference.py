"""Drug reference (ADR 024): the FDA label quoted for a named drug; never for a patient; no dosage
section by default; two drugs checked against each other's labels. No network: a fake store and a
fake RxNorm."""

import json

import pytest

from mcp_servers.drug_reference import server as dr
from mcp_servers.medication_review import labels as fda
from profiles.loader import load_profile

P = load_profile("clinic-assistant")
CFG = dr.load_config(P)

STORE = {"ingredients": {
    "apixaban": {"set_id": "aaaa1111-2222-3333-4444-555566667777", "effective_time": "20250410", "title": "Apixaban",
                 "brand_names": ["Eliquis"], "classes": ["Factor Xa Inhibitor"],
                 "sections": {"indications_and_usage": ["ELIQUIS is indicated to reduce the risk of stroke in nonvalvular atrial fibrillation."],
                              "boxed_warning": ["WARNING: PREMATURE DISCONTINUATION INCREASES THROMBOTIC RISK."],
                              "contraindications": ["Active pathological bleeding. Severe hypersensitivity."],
                              "drug_interactions": ["Combined P-gp and strong CYP3A4 inhibitors increase exposure. NSAIDs such as ibuprofen increase bleeding risk."],
                              "use_in_specific_populations": ["Renal impairment: no dose adjustment is recommended for mild or moderate impairment. " * 30],
                              "dosage_and_administration": ["5 mg twice daily."]}},
    "ibuprofen": {"set_id": "bbbb1111-2222-3333-4444-555566667777", "effective_time": "20240101", "title": "Ibuprofen",
                  "brand_names": ["Advil"], "classes": ["Nonsteroidal Anti-inflammatory Drug"],
                  "sections": {"indications_and_usage": ["Relief of mild to moderate pain."],
                               "drug_interactions": ["Anticoagulants: synergistic effect on bleeding."]}},
}, "lookups": {}}


def test_config_quotes_listed_sections_only_and_never_dosage_by_default():
    assert CFG.fetch == "on_demand" and "dosage_and_administration" not in CFG.sections
    assert CFG.default_sections == ("indications_and_usage", "boxed_warning", "contraindications")
    assert P.tools["drug_reference"].answer == "direct" and not P.tools["drug_reference"].receives_identifiers
    assert "drug_reference" in P.roles["clinician"].tools and "drug_reference" in P.roles["nurse"].tools
    assert "drug_reference" not in P.roles["front_desk"].tools


def test_a_question_about_a_person_is_refused_and_nothing_is_looked_up():
    out = dr.reference("what does the label say about [PERSON_1]'s warfarin", CFG, json.loads(json.dumps(STORE)))
    assert out["results"] == [] and "not patients" in out["answer"] and "medication review" in out["answer"]


def test_the_sections_asked_for_are_quoted_with_version_and_link(tmp_path):
    cfg = dr.Config(tmp_path / "s.json", "never", CFG.sections, CFG.default_sections, 1500, 2, CFG.not_a_drug)
    store = json.loads(json.dumps(STORE))
    out = dr.reference("What does the label say about apixaban in renal impairment?", cfg, store)
    a = out["answer"]
    assert a.startswith("FDA label for Apixaban (Eliquis), version 2025-04-10 [lbl-aaaa1111]")
    assert "Use in specific populations:" in a and "Renal impairment: no dose adjustment" in a
    assert "Indications and usage:" not in a                        # only the section asked for
    assert "dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=aaaa1111-2222-3333-4444-555566667777" in a
    assert "(cut; the full section is on DailyMed)" in a             # 30 repetitions > 1500 chars
    assert [r["id"] for r in out["results"]] == ["lbl-aaaa1111"] and P.source_id_pattern.fullmatch("lbl-aaaa1111")
    assert "not advice" in a and "not a recommendation" in a
    # the default sections when none is named; a brand name finds the drug
    a = dr.reference("tell me about eliquis", cfg, store)["answer"]
    assert "Indications and usage:" in a and "Boxed warning:" in a and "Contraindications:" in a


def test_dosage_is_never_quoted_and_the_answer_says_why(tmp_path):
    cfg = dr.Config(tmp_path / "s.json", "never", CFG.sections, CFG.default_sections, 1500, 2, CFG.not_a_drug)
    a = dr.reference("what is the apixaban label dosing", cfg, json.loads(json.dumps(STORE)))["answer"]
    assert "5 mg twice daily" not in a and "Dosage is not quoted here" in a


def test_two_drugs_are_checked_against_each_others_labels(tmp_path):
    cfg = dr.Config(tmp_path / "s.json", "never", CFG.sections, CFG.default_sections, 1500, 2, CFG.not_a_drug)
    a = dr.reference("apixaban and ibuprofen interactions", cfg, json.loads(json.dumps(STORE)))["answer"]
    assert "Apixaban and Ibuprofen together:" in a
    assert "Apixaban label, Drug interactions [lbl-aaaa1111], mentions \"ibuprofen\"" in a
    assert "Ibuprofen label" not in a.split("together:")[1] or "Anticoagulants" in a   # no class words for Factor Xa in the fake
    # with the review's class words, the other direction is found too
    cfg2 = dr.Config(tmp_path / "s.json", "never", CFG.sections, CFG.default_sections, 1500, 2, CFG.not_a_drug,
                     {"factor xa inhibitor": ["anticoagulant", "anticoagulants"]})
    a = dr.reference("apixaban and ibuprofen interactions", cfg2, json.loads(json.dumps(STORE)))["answer"]
    assert "Ibuprofen label, Drug interactions [lbl-bbbb1111], mentions \"Anticoagulants\"" in a
    assert "Vitamin K Antagonist".lower() in CFG.class_terms                   # loaded from medication_review.yaml


def test_unknown_words_are_looked_up_in_rxnorm_only_when_on_demand_and_poor_matches_rejected(tmp_path, monkeypatch):
    calls = []

    def fake_rxnorm(name, strict=False):
        assert strict
        calls.append(name)
        return ("123", ["sertraline"]) if name == "zoloft" else (None, [])
    monkeypatch.setattr(fda, "rxnorm", fake_rxnorm)
    monkeypatch.setattr(fda, "label", lambda ing, sections: {"set_id": "cccc1111-0000-0000-0000-000000000000",
                                                             "effective_time": "20230505", "title": "Sertraline",
                                                             "brand_names": ["Zoloft"], "classes": [],
                                                             "sections": {"indications_and_usage": ["Major depressive disorder."]}})
    cfg = dr.Config(tmp_path / "s.json", "on_demand", CFG.sections, CFG.default_sections, 1500, 2, CFG.not_a_drug)
    store = {"ingredients": {}, "lookups": {}}
    out = dr.reference("side effects of zoloft", cfg, store)
    assert calls == ["zoloft"]                                      # "side", "effects" are not-a-drug words
    assert out["fetched"] == [{"ingredient": "sertraline", "set_id": "cccc1111-0000-0000-0000-000000000000", "effective_time": "20230505"}]
    assert "fetched today" in out["answer"] and (tmp_path / "s.json").exists()
    assert "no adverse reactions section on file" in out["answer"].lower()
    # the store remembers the word, so the next question makes no lookup; an ordinary word is remembered as not a drug
    out = dr.reference("tell me about zoloft and bananas", cfg, store)
    assert calls == ["zoloft", "bananas"] and store["lookups"]["bananas"] == []
    assert "Sertraline" in out["answer"]
    cfg_never = dr.Config(tmp_path / "s.json", "never", CFG.sections, CFG.default_sections, 1500, 2, CFG.not_a_drug)
    out = dr.reference("tell me about lisinopril", cfg_never, {"ingredients": {}, "lookups": {}})
    assert calls[-1] == "bananas" and "couldn't tell which medication" in out["answer"]


def test_the_trigger_the_exemption_and_the_decision_line():
    from agents.tool_choice import tools_to_call
    from guardrails.rules import GuardrailDecision, check_input, classifier_exemption
    flagged = GuardrailDecision(False, "medical_advice", "classifier")
    for q in ("What does the label say about apixaban in renal impairment", "contraindications of amiodarone",
              "warfarin and ibuprofen interactions", "look up apixaban"):
        assert tools_to_call(P, "planner", q)[1] == ["drug_reference"], q
        assert check_input(q, P).allowed
        assert classifier_exemption(q, flagged, P) is not None, q
    assert tools_to_call(P, "planner", "What medications is Harold Brinkley taking?")[1] == []
    # a decision stays a decision: the rule blocks a dose question, and no exemption applies to "should we"
    # or "can I give", so the classifier's flag stands (a drug name alone is not a rule term: classifier territory)
    assert not check_input("How much apixaban dose should we give, what does the label say?", P).allowed
    assert classifier_exemption("How much apixaban should we give, what does the label say?", flagged, P) is None
    assert classifier_exemption("Can I give warfarin with ibuprofen, what does the label say", flagged, P) is None


def test_medication_names_are_never_masked_as_people():
    assert "apixaban" in P.privacy_allow_list and "Apixaban" in P.privacy_allow_list and "ELIQUIS" in P.privacy_allow_list
    mask = pytest.importorskip("privacy.masking").mask            # needs Presidio installed
    masked, mapping = mask("what does the label say about apixaban and Eliquis?", P)
    assert "[PERSON" not in masked and mapping == {}
    masked, mapping = mask("what does the label say about Harold Brinkley's apixaban?", P)
    assert "[PERSON_1]" in masked and "apixaban" in masked          # the person is masked, the drug is not


def test_rxnorm_strict_accepts_the_word_or_a_near_spelling_not_a_loose_match(monkeypatch):
    reply = {"approximateGroup": {"candidate": [{"rxcui": "1364430", "score": "13.4", "name": "Apixaban"}]}}
    monkeypatch.setattr(fda, "get", lambda url: reply if "approximateTerm" in url else
                        {"relatedGroup": {"conceptGroup": [{"conceptProperties": [{"name": "apixaban"}]}]}})
    assert fda.rxnorm("apixaban", strict=True) == ("1364430", ["apixaban"])
    assert fda.rxnorm("apixiban", strict=True) == ("1364430", ["apixaban"])      # a typo
    assert fda.rxnorm("impairment", strict=True) == (None, [])                   # RxNav's loose match, rejected
    assert fda.rxnorm("impairment") == ("1364430", ["apixaban"])                 # the review's lenient use
