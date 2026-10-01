from privacy.masking import mask, unmask


def test_name_phone_and_email_are_masked_and_restored():
    text = "Hi, I'm John Smith. Call me at 416-555-0199 or john@example.com."
    masked, mapping = mask(text)

    assert "John Smith" not in masked
    assert "416-555-0199" not in masked
    assert "john@example.com" not in masked
    assert unmask(masked, mapping) == text


def test_same_name_gets_same_token():
    text = "John Smith said hello. Later, John Smith left."
    masked, mapping = mask(text)

    assert masked.count("[PERSON_1]") == 2
    assert unmask(masked, mapping) == text


def test_text_without_personal_details_is_unchanged():
    text = "Help me plan a focused morning."
    masked, mapping = mask(text)

    assert masked == text
    assert mapping == {}


def test_medication_names_are_not_masked():
    for text in [
        "How much Adderall should I take?",
        "Can I double my Vyvanse dose before an exam?",
        "Is it safe to drink alcohol on Ritalin?",
    ]:
        masked, mapping = mask(text)
        assert masked == text, f"Medication name was masked in: {text}"
        assert mapping == {}


def test_medication_allowed_but_real_name_still_masked():
    text = "I'm Sarah Lee and I take Concerta."
    masked, mapping = mask(text)

    assert "Concerta" in masked
    assert "Sarah Lee" not in masked