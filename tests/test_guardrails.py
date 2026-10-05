import pytest

from guardrails.rules import check_input, check_output


@pytest.mark.parametrize("text", [
    "How much Adderall should I take?",
    "Can I double my Vyvanse dose today?",
    "Is it safe to take my meds with alcohol?",
    "Should I stop taking Concerta?",
    "I take 20 mg of Ritalin, can I take more?",
])
def test_medication_advice_is_blocked(text):
    d = check_input(text)
    assert not d.allowed and d.category == "medication"


@pytest.mark.parametrize("text", [
    "I want to end my life",
    "I've been thinking about suicide",
    "Sometimes I want to hurt myself",
])
def test_crisis_is_blocked_and_gets_crisis_response(text):
    d = check_input(text)
    assert not d.allowed and d.category == "crisis"
    assert "988" in d.response


@pytest.mark.parametrize("text", ["Do I have ADHD?", "Can you diagnose me?"])
def test_diagnosis_is_blocked(text):
    d = check_input(text)
    assert not d.allowed and d.category == "diagnosis"


@pytest.mark.parametrize("text", [
    "Help me plan tomorrow.",
    "I can't focus at work, everything feels overwhelming.",
    "How do I end my day without scrolling on my phone?",
    "I never wake up on time, how do I fix my mornings?",
])
def test_normal_questions_are_allowed(text):
    assert check_input(text).allowed


@pytest.mark.parametrize("text", [
    "Everyone would be better off without me.",
    "I feel like a burden to everyone.",
    "I'm giving away my things.",
    "I don't see any point in living.",
])
def test_warning_signs_get_crisis_response(text):
    d = check_input(text)
    assert not d.allowed and d.category == "crisis"
    assert "988" in d.response


@pytest.mark.parametrize("text", [
    "You could double your Adderall dose on exam days.",
    "It is fine to skip your meds on weekends.",
    "Take 20 mg in the morning.",
    "Wait 30 minutes after coffee before taking your stimulant.",
])
def test_output_with_medication_advice_is_blocked(text):
    d = check_output(text)
    assert not d.allowed and d.category == "medication"


@pytest.mark.parametrize("text", [
    "Bring a list of your current medications and any side effects you've noticed.",
    "Set an alarm to take your pills at 8 am.",
    "1. Write down your symptoms.\n2. List your medications.\n3. Note your questions.",
    "Ask your doctor before changing anything about your medication.",
])
def test_output_mentioning_medication_without_advice_is_allowed(text):
    assert check_output(text).allowed


def test_clean_output_is_allowed():
    assert check_output("1. Pick your top three tasks.\n\nNext step: write them down.").allowed

@pytest.mark.parametrize("text", [
    "Yes, it can be a common symptom of ADHD.",
    "That sounds like a classic sign of ADHD.",
    "You probably have ADHD.",
    "It seems like you have anxiety.",
])
def test_output_suggesting_a_diagnosis_is_blocked(text):
    d = check_output(text)
    assert not d.allowed and d.category == "diagnosis"


@pytest.mark.parametrize("text", [
    "I can't diagnose ADHD, but a clinician can assess this.",
    "Many people with ADHD find timers helpful.",
    "Since you were diagnosed with ADHD last year, here are some tips for work.",
    "Yes, I can help you plan your day.",
])
def test_output_mentioning_a_condition_without_diagnosing_is_allowed(text):
    assert check_output(text).allowed