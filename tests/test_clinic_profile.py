"""The clinic profile blocks clinical decisions but lets staff read what records say."""

import pytest

from guardrails.rules import check_input, check_output
from profiles.loader import load_profile

P = load_profile("clinic-assistant")


@pytest.mark.parametrize("text", [
    "The patient said she wants to die",
    "Mrs Lee told me she took too many pills",
])
def test_safety_risk_gets_the_staff_protocol(text):
    d = check_input(text, P)
    assert not d.allowed and d.category == "crisis"
    assert "on-call clinician" in d.response and "911" in d.response


@pytest.mark.parametrize("text", [
    "How much insulin should we give Mr Smith?",
    "Can I increase her metformin dose?",
    "Is it safe to combine these meds?",
])
def test_medication_decisions_are_blocked(text):
    assert check_input(text, P).category == "medication"


@pytest.mark.parametrize("text", ["Does the patient have an infection?", "Can you interpret these labs?"])
def test_diagnosis_requests_are_blocked(text):
    assert check_input(text, P).category == "diagnosis"


@pytest.mark.parametrize("text", [
    "What medications is Jody Hickle taking?",
    "What dose of acetaminophen is on her med list?",
    "When is Jody Hickle's next appointment?",
    "Does Jody have an outstanding balance?",
    "What is our cancellation policy?",
    "This billing system is killing me",
])
def test_normal_staff_questions_pass(text):
    assert check_input(text, P).allowed


@pytest.mark.parametrize("text", [
    "Current medications: Acetaminophen 325 MG Oral Tablet (record med-12).",
    "It looks like her next appointment is on Monday at 9:30.",
    "It seems like she has an appointment on Friday.",
    "That is consistent with clinic policy kb-cl-002.",
    "The last visit was for acute bronchitis (record enc-41).",
])
def test_reading_what_records_say_is_allowed(text):
    assert check_output(text, P).allowed


@pytest.mark.parametrize("text", [
    "You should increase the dose of insulin to 10 units.",
    "The patient likely has diabetes.",
    "This sounds like an infection.",
    "These symptoms are consistent with pneumonia.",
    "It seems like she has a urinary tract infection.",
])
def test_clinical_advice_in_answers_is_blocked(text):
    assert not check_output(text, P).allowed


def test_patient_data_never_goes_external():
    r = P.routing
    assert "patient_record" in r["never_external_labels"]
    assert "PERSON" in r["never_external_entities"]
    assert r["send_external_when"] == "no_personal_info"