"""The crisis replies name help for abuse as well as suicide, and a review run cannot be credited to a
guard model that did not answer it. No running system or model is needed."""

import pytest

from eval import run_adhd_review as rr
from profiles.loader import load_profile

PROFILES = ("adhd-assistant", "clinic-assistant", "county-benefits")


@pytest.mark.parametrize("name", PROFILES)
def test_the_crisis_reply_covers_suicide_and_abuse(name):
    reply = load_profile(name).response("crisis")
    assert "988" in reply and "911" in reply
    assert "1-800-799-7233" in reply and "88788" in reply        # National Domestic Violence Hotline


def test_the_review_run_names_the_guard_from_the_profile():
    assert rr.expected_guard("adhd-assistant") == load_profile("adhd-assistant").classifier.guard_model


def test_a_reply_from_another_guard_stops_the_run():
    assert rr.check_guard({"guards": {"question": "guard-modernbert-v4"}}, "guard-modernbert-v4") == "guard-modernbert-v4"
    with pytest.raises(SystemExit, match="Restart"):
        rr.check_guard({"guards": {"question": "guard-modernbert-v3"}}, "guard-modernbert-v4")
    with pytest.raises(SystemExit, match="Restart"):
        rr.check_guard({}, "guard-modernbert-v4")                 # a system too old to say