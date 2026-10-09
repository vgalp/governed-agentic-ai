"""Training data for the guard classifier: the unseen profile and the frozen test set stay
out of it (classifier/generate.py, classifier/clean.py). No model is called here."""

import json
import random
import sys

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


def test_the_prompt_never_shows_test_names_and_asks_for_json():
    names = generate.test_names()
    assert "Marta Okafor" in names and len(names) > 50
    for profile, label, angle, text, _ in generate.plan(1.0):
        content = generate.prompt(profile, label, text, 10, random.Random(1))[1]["content"]
        assert '"messages"' in content and "{" not in generate.instruction(profile, text)
        assert not any(n in content for n in names), (profile, angle)


def test_examples_in_the_prompts_are_not_test_messages():
    import difflib
    test = [t["text"].lower() for t in TEST]
    for profile, label, angle, text, _ in generate.plan(1.0):
        instr = generate.instruction(profile, text).lower()
        for t in test:
            assert t not in instr
        for example in instr.split('"')[1::2]:
            if len(example.split()) >= 4:
                assert max(difflib.SequenceMatcher(None, example, t).ratio() for t in test) < 0.75, example


class FakeOllama:
    """Answers generation requests with fixed messages, and labelling requests with fixed labels."""

    def __init__(self, messages, labels):
        self.messages, self.labels = messages, labels

    def __call__(self, url, json=None, timeout=None):
        is_judge = "labels" in json["messages"][-1]["content"]
        content = ({"labels": self.labels} if is_judge else {"messages": self.messages})
        resp = type("R", (), {})()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"message": {"content": __import__("json").dumps(content)}}
        return resp


def test_judge_reads_labels_and_fails_safe(monkeypatch):
    monkeypatch.setattr(generate.requests, "post", FakeOllama([], ["allow", "crisis"]))
    assert generate.judge("m", "clinic-assistant", ["one two three", "four five six"], 1) == ["allow", "crisis"]
    assert generate.judge("m", "clinic-assistant", ["only one message"], 1) == [None]     # wrong count


def test_the_second_look_knows_the_dose_and_overdose_rules(monkeypatch):
    sent = []

    def capture(url, json=None, timeout=None):
        sent.append(json["messages"][-1]["content"])
        return FakeOllama([], ["decision"])(url, json, timeout)
    monkeypatch.setattr(generate.requests, "post", capture)
    generate.judge("m", "clinic-assistant", ["is 80mg a lot for an adult"], 1)
    generate.judge("m", "county-benefits", ["is Ray eligible for SNAP"], 1)
    assert generate.HEALTH_NOTE in sent[0] and generate.HEALTH_NOTE not in sent[1]
    assert "deadly" in sent[0] and "Crisis wins" in sent[1]


def test_messages_the_second_look_disagrees_with_are_set_aside(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "OUT", tmp_path / "raw.jsonl")
    monkeypatch.setattr(generate, "REJECTED", tmp_path / "rejected.jsonl")
    monkeypatch.setattr(generate, "plan", lambda scale: [("clinic-assistant", "crisis", "reported", "x", 2)])
    msgs = ["Ruth said she wants to end her life", "Ruth seemed a bit tired at check-in", "Ray said he took all his pills"]
    monkeypatch.setattr(generate.requests, "post", FakeOllama(msgs, ["crisis", "allow", "crisis"]))
    monkeypatch.setattr(sys, "argv", ["generate", "--scale", "1"])
    generate.main()
    kept = [json.loads(line)["text"] for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    rejected = [json.loads(line) for line in (tmp_path / "rejected.jsonl").read_text().splitlines()]
    assert kept == [msgs[0], msgs[2]]
    assert rejected == [{"profile": "clinic-assistant", "label": "crisis", "angle": "reported", "text": msgs[1],
                         "generator": "qwen2.5:7b", "judge": "allow"}]


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


@pytest.mark.parametrize("reply", [
    '{"messages": ["Can you plan my Saturday chores?", "Help me write a note to my boss"]}',
    '["Can you plan my Saturday chores?", "Help me write a note to my boss"]',
    '{"1": "Can you plan my Saturday chores?", "2": "Help me write a note to my boss"}',
    '{"messages": [{"text": "Can you plan my Saturday chores?"}, {"message": "Help me write a note to my boss"}]}',
    'Sure! Here they are: ["Can you plan my Saturday chores?", "Help me write a note to my boss"] Hope it helps.',
    '{"messages": [\n1.50303016, "Can you plan my Saturday chores?",\n2.50303018, "Help me write a note to my boss"\n]}',
    '{"messages": ["Can you plan my Saturday chores?", "Help me write a note to my boss", "Remind me to',
])
def test_messages_are_read_from_any_reply_shape(reply):
    assert generate.extract(reply) == ["Can you plan my Saturday chores?", "Help me write a note to my boss"]


@pytest.mark.parametrize("reply", ["", "not json at all", '{"error": "no"}', '{"messages": ["hi", 3]}'])
def test_unreadable_replies_give_nothing(reply):
    assert generate.extract(reply) == []