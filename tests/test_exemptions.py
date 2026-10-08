"""Narrow overrides of input classifier flags, invented fill-in fields, and the
grounding check (an answer may only cite sources it was given)."""

import shutil

import pytest
import yaml

from guardrails.grounding import check_citations
from guardrails.rules import GuardrailDecision, check_output, classifier_exemption
from profiles.loader import ProfileError, load_profile, load_profile_from

CLINIC = load_profile("clinic-assistant")
S2 = GuardrailDecision(False, "medical_advice", "classifier: S2 clinical decision", "x")


@pytest.mark.parametrize("q", [
    "What medications is Chester Aufderhar taking?",
    "List the current medications for Jesse Wolf.",
    "Tell me about Chester Aufderhar's medications",
    "What prescriptions are on file for Ann Lee?",
])
def test_pure_record_lookups_are_exempt(q):
    assert classifier_exemption(q, S2, CLINIC)


@pytest.mark.parametrize("q", [
    "What medication do I need to take for ADHD?",
    "Should we increase his lisinopril?",
    "Can I double her metformin tonight?",
    "What should we prescribe for her anxiety?",
    "What medications is Chester taking and should we stop any?",
    "Which medication is best for her pain?",
    "What meds can I give for a headache?",
    "Is it okay to give him ibuprofen with his blood pressure pills?",
])
def test_decision_words_keep_the_flag(q):
    assert classifier_exemption(q, S2, CLINIC) is None


def test_crisis_flags_are_never_exempt():
    crisis = GuardrailDecision(False, "crisis", "classifier: S1", "x")
    assert classifier_exemption("What medications is she taking? She wants to die.", crisis, CLINIC) is None


def test_profile_without_exemptions_never_overrides():
    adhd = load_profile("adhd-assistant")
    assert adhd.classifier_exemptions == () and adhd.source_id_pattern is None
    assert classifier_exemption("What medications is she taking?", S2, adhd) is None


# --- answers -------------------------------------------------------------------

def test_invented_fill_in_field_is_blocked():
    d = check_output("The patient's current metformin dose is [METFORMIN_DOSE].", CLINIC)
    assert not d.allowed and d.category == "ungrounded"


def test_real_masking_tokens_are_not_mistaken_for_invented_fields():
    assert check_output("Booked for [PERSON_1] on Monday.", CLINIC).allowed


def test_citing_a_source_it_was_not_given_is_blocked():
    d = check_citations("Take it with food (kb-cl-001).", ["med-9"], CLINIC)
    assert not d.allowed and "kb-cl-001" in d.reason


def test_citing_only_given_sources_passes():
    assert check_citations("Lisinopril 10 MG Oral Tablet (med-9).", ["med-9", "pt-d3b9ef3f"], CLINIC).allowed
    assert check_citations("No medications are on file.", [], CLINIC).allowed


def test_profile_without_citations_skips_the_check():
    assert check_citations("anything (kb-cl-001)", [], load_profile("adhd-assistant")).allowed


# --- the loader keeps exemptions narrow -----------------------------------------

@pytest.fixture
def copy_of_clinic(tmp_path):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(CLINIC.path, dst, ignore=shutil.ignore_patterns("clinic.db", "sample_csv"))
    return dst


def _edit(path, change):
    d = yaml.safe_load(path.read_text())
    change(d)
    path.write_text(yaml.safe_dump(d, sort_keys=False))


def test_crisis_exemption_is_rejected(copy_of_clinic):
    _edit(copy_of_clinic / "rules.yaml", lambda d: d["classifier_exemptions"][0].update(category="crisis"))
    with pytest.raises(ProfileError, match="crisis flags can never be overridden"):
        load_profile_from(copy_of_clinic)


def test_exemption_without_cancelling_words_is_rejected(copy_of_clinic):
    _edit(copy_of_clinic / "rules.yaml", lambda d: d["classifier_exemptions"][0].pop("none"))
    with pytest.raises(ProfileError, match="needs 'none'"):
        load_profile_from(copy_of_clinic)