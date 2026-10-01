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