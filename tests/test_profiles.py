"""Profiles load, validate, and fail clearly when something is missing."""

import shutil

import pytest
import yaml

from guardrails.classifier import parse_guard_output, to_decision
from guardrails.rules import check_input, check_output
from profiles.loader import (ProfileError, available_profiles, load_profile,
                             load_profile_from, read_prompt)


def test_every_profile_loads():
    names = available_profiles()
    assert "adhd-assistant" in names
    for n in names:
        load_profile(n)


def test_adhd_profile_contents():
    p = load_profile("adhd-assistant")
    assert p.model["provider"] == "ollama"
    assert p.data_policy["external_models_allowed"] is False
    assert p.agents == {"planner": ("search_knowledge",)}
    assert p.policy_data() == {"agents": {"planner": {"tools": ["search_knowledge"]}}}
    assert [c.category for c in p.classifier.categories][0] == "crisis"   # crisis first
    assert "988" in p.response("crisis")
    assert "\n" not in p.system_prompt          # wrapped lines are joined


def test_profile_drives_the_rules():
    p = load_profile("adhd-assistant")
    d = check_input("How much Adderall should I take?", p)
    assert d.category == "medication" and d.response == p.response("medication")
    assert check_output("Take 20 mg in the morning.", p).category == "medication"
    # sentence scope: medication and advice in different sentences is allowed
    assert check_output("Bring a list of your medications. Then double-check the time.", p).allowed


def test_classifier_mapping_comes_from_profile():
    p = load_profile("adhd-assistant")
    assert to_decision(parse_guard_output("unsafe\nS2,S1"), p).category == "crisis"
    assert to_decision(parse_guard_output("unsafe\nS9"), p).category == p.classifier.default_category


def test_read_prompt_keeps_paragraphs(tmp_path):
    f = tmp_path / "prompt.md"
    f.write_text("Line one\nline two.\n\nSecond paragraph.\n")
    assert read_prompt(f) == "Line one line two.\n\nSecond paragraph."


# --- broken profiles fail at load time, with the reason -----------------------

@pytest.fixture
def copy_of_adhd(tmp_path):
    src = load_profile("adhd-assistant").path
    dst = tmp_path / "adhd-assistant"
    shutil.copytree(src, dst)
    return dst


def _edit_yaml(path, change):
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_missing_response_is_rejected(copy_of_adhd):
    _edit_yaml(copy_of_adhd / "responses.yaml", lambda d: d.pop("crisis"))
    with pytest.raises(ProfileError, match="no response for crisis"):
        load_profile_from(copy_of_adhd)


def test_unknown_pattern_is_rejected(copy_of_adhd):
    _edit_yaml(copy_of_adhd / "rules.yaml", lambda d: d["input"][0].update(any=["nope"]))
    with pytest.raises(ProfileError, match="unknown pattern 'nope'"):
        load_profile_from(copy_of_adhd)


def test_invalid_regex_is_rejected(copy_of_adhd):
    _edit_yaml(copy_of_adhd / "rules.yaml", lambda d: d["patterns"].update(bad={"regex": "("}))
    with pytest.raises(ProfileError, match="not a valid regex"):
        load_profile_from(copy_of_adhd)


def test_name_must_match_folder(copy_of_adhd):
    _edit_yaml(copy_of_adhd / "profile.yaml", lambda d: d.update(name="something-else"))
    with pytest.raises(ProfileError, match="must match the folder name"):
        load_profile_from(copy_of_adhd)


def test_files_outside_the_profile_are_rejected(copy_of_adhd):
    _edit_yaml(copy_of_adhd / "profile.yaml", lambda d: d.update(prompt="../../README.md"))
    with pytest.raises(ProfileError, match="outside the profile folder"):
        load_profile_from(copy_of_adhd)


def test_unknown_profile_name_is_rejected():
    with pytest.raises(ProfileError):
        load_profile("../etc")
