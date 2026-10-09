"""Training data for the guard classifier: the unseen profile and the frozen test set stay
out of it (classifier/generate.py, classifier/clean.py). No model is called here."""

import json
import random

import pytest

from classifier import clean, generate

TEST = [json.loads(line) for line in generate.TEST_SET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_unseen_profile_is_never_used_for_training():
    assert "housing-office" not in generate.POLICIES
    assert all(p != "housing-office" for p, *_ in generate.plan(1.0))


def test_training_policies_match_the_frozen_ones():
    import yaml
    frozen = yaml.safe_load((generate.TEST_SET.parent / "guard_test_policies.yaml").read_text())
    assert all(frozen[k] == v for k, v in generate.POLICIES.items())


def test_the_plan_covers_every_label_for_every_profile():
    cells = generate.plan(1.0)
    for profile in generate.POLICIES:
        assert {label for p, label, *_ in cells if p == profile} == set(clean.LABELS)
    assert 2500 <= sum(c[4] for c in cells) <= 3200


def test_the_prompt_forbids_test_names_and_asks_for_json():
    names = sorted(generate.test_names())
    assert "Marta Okafor" in names and len(names) > 50
    msgs = generate.prompt("county-benefits", "crisis", "reports that another person...", 10, names[:5],
                           random.Random(1))
    assert names[0] in msgs[1]["content"] and '"messages"' in msgs[1]["content"]


def row(text, label="allow", profile="clinic-assistant"):
    return {"profile": profile, "label": label, "angle": "x", "text": text, "generator": "test"}


def test_clean_drops_duplicates_near_copies_and_test_names():
    held = clean.held_out()
    rows = [
        row("Can you list the open referrals for my patient next week?"),
        row("can you list the open referrals for my patient, next week"),          # duplicate
        row(TEST[0]["text"] + " please"),                                         # near copy of a test message
        row("When is Marta Okafor coming in again?"),                             # test-set name
        row("Write a note about the parking lot being closed on Monday."),
        row("Something", label="maybe"),                                          # bad label
    ]
    kept, dropped = clean.clean(rows, held, generate.test_names())
    assert [r["text"] for r in kept] == [rows[0]["text"], rows[4]["text"]]
    assert dropped == {"duplicate": 1, "too close to a held-out message": 1, "test-set name": 1, "bad label": 1}


def test_nothing_in_the_test_set_survives_cleaning():
    rows = [row(t["text"], t["label"], "clinic-assistant") for t in TEST[:40]]
    kept, _ = clean.clean(rows, clean.held_out(), set())
    assert kept == []


def test_split_keeps_every_cell_in_dev():
    rows = [{**row(f"message number {i}", label), "id": f"g{i}"} for i in range(60)
            for label in [clean.LABELS[i % 3]]]
    train, dev = clean.split(rows, seed=1)
    assert len(train) + len(dev) == 60 and {r["label"] for r in dev} == set(clean.LABELS)


def test_review_corrections_are_applied(tmp_path, monkeypatch):
    for name in ("TRAIN", "DEV", "REVIEW"):
        monkeypatch.setattr(clean, name, tmp_path / getattr(clean, name).name)
    rows = [{**row(f"msg {i}"), "id": f"g{i}"} for i in range(4)]
    clean.write_jsonl(clean.TRAIN, rows[:3])
    clean.write_jsonl(clean.DEV, rows[3:])
    clean.REVIEW.write_text("id,profile,angle,label,text,your_label,note\n"
                            "g0,clinic-assistant,x,allow,msg 0,,\n"
                            "g1,clinic-assistant,x,allow,msg 1,crisis,\n"
                            "g3,clinic-assistant,x,allow,msg 3,drop,\n")
    clean.apply_review()
    train = {r["id"]: r for r in clean.read_jsonl(clean.TRAIN)}
    assert train["g0"]["checked"] == "confirmed" and train["g1"]["label"] == "crisis"
    assert "checked" not in train["g2"] and clean.read_jsonl(clean.DEV) == []


def test_bad_review_labels_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(clean, "REVIEW", tmp_path / "review.csv")
    clean.REVIEW.write_text("id,profile,angle,label,text,your_label,note\ng0,c,x,allow,t,maybe,\n")
    with pytest.raises(SystemExit, match="maybe"):
        clean.apply_review()