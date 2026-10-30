"""Training data for the guard classifier: the unseen profile and the frozen test set stay
out of it (classifier/generate.py, classifier/clean.py). No model is called here."""

import builtins
import json
import random
import sys

import pytest

from classifier import clean, generate

TEST = [json.loads(line) for line in generate.TEST_SET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_unseen_profiles_are_never_used_for_training():
    assert not generate.UNSEEN & set(generate.POLICIES)
    assert all(p not in generate.UNSEEN for p, *_ in generate.plan(1.0))
    assert generate.TEST_SET in generate.TEST_SETS          # names from every test set are kept out


def test_training_policies_match_the_frozen_ones():
    import yaml
    frozen = yaml.safe_load((generate.TEST_SET.parent / "guard_test_policies.yaml").read_text())
    assert all(frozen[k] == v for k, v in generate.POLICIES.items())


def test_the_plan_covers_every_label_for_every_profile():
    cells = generate.plan(1.0)
    for profile in generate.POLICIES:
        assert {label for p, label, *_ in cells if p == profile} == set(clean.LABELS)
    assert 2500 <= sum(c[4] for c in cells) <= 5500        # v6 added about 870


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
    for name in ("TRAIN", "DEV", "REVIEW", "REVIEWED"):
        monkeypatch.setattr(clean, name, tmp_path / getattr(clean, name).name)
    rows = [{**row(f"msg {i}"), "id": f"g{i}"} for i in range(4)]
    clean.write_jsonl(clean.TRAIN, rows[:3])
    clean.write_jsonl(clean.DEV, rows[3:])
    clean.REVIEW.write_text("id,profile,angle,label,text,your_label,note\n"
                            "g0,clinic-assistant,x,allow,msg 0,,\n"
                            "g1,clinic-assistant,x,allow,msg 1,crisis,\n"
                            "g3,clinic-assistant,x,allow,msg 3,drop,\n")
    (tmp_path / "review_progress.json").write_text('{"done": 3, "total": 3}')
    clean.apply_review("Claude; checked by a person")
    train = {r["id"]: r for r in clean.read_jsonl(clean.TRAIN)}
    assert train["g0"]["checked"] == "confirmed" and train["g1"]["label"] == "crisis"
    assert "checked" not in train["g2"] and clean.read_jsonl(clean.DEV) == []
    recorded = clean.read_reviewed()
    assert recorded["g1"]["your_label"] == "crisis" and recorded["g3"]["your_label"] == "drop"
    assert recorded["g0"]["your_label"] == "" and recorded["g0"]["reviewer"] == "Claude; checked by a person"


def test_bad_review_labels_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(clean, "REVIEW", tmp_path / "review.csv")
    clean.REVIEW.write_text("id,profile,angle,label,text,your_label,note\ng0,c,x,allow,t,maybe,\n")
    (tmp_path / "review_progress.json").write_text('{"done": 1, "total": 1}')
    with pytest.raises(SystemExit, match="maybe"):
        clean.apply_review("someone")
    with pytest.raises(SystemExit, match="who reviewed"):
        clean.apply_review(" ")


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


def test_only_explicit_crisis_messages_are_rescued():
    rejected = [
        {"profile": "clinic-assistant", "label": "crisis", "angle": "means", "text": "is 50 zoloft lethal", "judge": "decision"},
        {"profile": "county-benefits", "label": "crisis", "angle": "indirect", "text": "says she's a burden to everyone",
         "judge": "allow"},
        {"profile": "adhd-assistant", "label": "crisis", "angle": "embedded", "text": "make a budget, money is tight",
         "judge": "allow"},
        {"profile": "clinic-assistant", "label": "decision", "angle": "direct", "text": "is this dose deadly or fine",
         "judge": "allow"},
    ]
    got = clean.rescue(rejected)
    assert [r["text"] for r in got] == [rejected[0]["text"], rejected[1]["text"]]
    assert all(r["rescued"] and "judge" not in r and r["label"] == "crisis" for r in got)


def test_every_rescued_message_is_in_the_review(tmp_path, monkeypatch):
    import csv
    monkeypatch.setattr(clean, "REVIEW", tmp_path / "review.csv")
    rows = [{**row(f"plain message number {i}"), "id": f"g{i}"} for i in range(50)]
    rows += [{**row(f"rescued message {i} lethal", "crisis"), "id": f"r{i}", "rescued": True} for i in range(30)]
    clean.write_review(rows, seed=1)
    with clean.REVIEW.open() as f:
        ids = [r["id"] for r in csv.DictReader(f)]
    assert all(f"r{i}" in ids for i in range(30)) and len(ids) == 30 + clean.REVIEW_PER_CELL
    assert json.loads((tmp_path / "review_progress.json").read_text()) == {"done": 0, "total": len(ids)}


def test_an_unfinished_review_cannot_be_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(clean, "REVIEW", tmp_path / "review.csv")
    clean.REVIEW.write_text("id,profile,angle,label,text,your_label,note\ng0,c,x,allow,t,,\n")
    with pytest.raises(SystemExit, match="not finished"):
        clean.apply_review("someone")                         # never opened in the review tool
    (tmp_path / "review_progress.json").write_text('{"done": 0, "total": 1}')
    with pytest.raises(SystemExit, match="not finished"):
        clean.apply_review("someone")                         # opened, not finished


def test_the_review_tool_records_answers_and_resumes(tmp_path, monkeypatch):
    from classifier import review
    monkeypatch.setattr(clean, "REVIEW", tmp_path / "review.csv")
    clean.REVIEW.write_text("id,profile,angle,label,text,your_label,note\n"
                            "g0,c,x,allow,one,,\ng1,c,x,crisis,two,,\ng2,c,x,decision,three,,\n")
    keys = iter(["", "a", "q"])                               # keep, change to allow, quit
    monkeypatch.setattr(builtins, "input", lambda _: next(keys))
    review.main()
    rows, done = review.load()
    assert done == 2 and [r["your_label"] for r in rows] == ["", "allow", ""] and not review.finished()
    keys = iter(["x"])                                        # continue: drop the last one
    review.main()
    rows, done = review.load()
    assert done == 3 and rows[2]["your_label"] == "drop" and review.finished()


def test_choosing_the_given_label_counts_as_confirming():
    from classifier import review
    row = {"label": "crisis", "your_label": ""}
    assert review.answer(row, "c") and row["your_label"] == ""
    assert review.answer(row, "D") and row["your_label"] == "decision"
    assert not review.answer(row, "z")


def test_enter_keeps_an_answer_that_is_already_there():
    from classifier import review
    row = {"label": "crisis", "your_label": "allow"}           # filled in earlier, or by a first pass
    assert review.answer(row, "") and row["your_label"] == "allow"
    assert review.answer(row, "c") and row["your_label"] == ""  # back to the given label


def test_the_same_message_with_another_name_is_dropped():
    rows = [row("Can we deny Kimura Yuki's benefits?", "decision", "county-benefits"),
            row("Can we deny Jameson Kai benefits?", "decision", "county-benefits"),
            row("Mr. Rodriguez said he's tired of being a burden.", "crisis"),
            row("Luisa said she's tired of being a burden.", "crisis"),
            row("Can we approve Kimura Yuki's benefits?", "decision", "county-benefits"),
            row("What meds are listed for Aisha Patel?"),
            row("What meds are listed for Aisha Patel?", "decision")]   # other label: kept for the review
    kept, dropped = clean.clean(rows, [], set())
    assert [r["text"] for r in kept] == [rows[0]["text"], rows[2]["text"], rows[4]["text"], rows[5]["text"]]
    assert dropped == {"same template": 2, "duplicate": 1}


def test_reviews_are_kept_when_the_data_is_cleaned_again(tmp_path, monkeypatch):
    monkeypatch.setattr(clean, "REVIEWED", tmp_path / "reviewed.csv")
    clean.REVIEWED.write_text("id,profile,label,your_label,reviewer,note\n"
                              "g1,c,allow,crisis,someone,\ng2,c,allow,drop,someone,\ng3,c,allow,,someone,\n")
    rows = [{**row(f"msg {i}"), "id": f"g{i}"} for i in range(4)]
    out, dropped = clean.apply_decisions(rows, clean.read_reviewed())
    assert dropped == 1 and [r["id"] for r in out] == ["g0", "g1", "g3"]
    assert "checked" not in out[0] and out[1]["label"] == "crisis" and out[1]["checked"] == "corrected"
    assert out[2]["checked"] == "confirmed"


def test_only_kinds_of_message_never_reviewed_are_sampled(tmp_path, monkeypatch):
    import csv
    monkeypatch.setattr(clean, "REVIEW", tmp_path / "review.csv")
    old = [{**row(f"old message {i}"), "angle": "lookup", "id": f"o{i}"} for i in range(40)]
    for r in old[:5]:
        r["checked"] = "confirmed"
    new = [{**row(f"new message {i}"), "angle": "wordplay", "id": f"n{i}"} for i in range(40)]
    done = [{**row(f"rescued {i} lethal", "crisis"), "id": f"r{i}", "rescued": True, "checked": "confirmed"}
            for i in range(5)]
    assert clean.write_review(old + new + done, seed=1) == clean.REVIEW_PER_CELL
    with clean.REVIEW.open() as f:
        assert all(r["id"].startswith("n") for r in csv.DictReader(f))


def test_the_wordplay_angle_covers_every_profile_with_its_own_words():
    angle = next(a for a in generate.ANGLES if a[1] == "wordplay")
    assert angle[0] == "allow" and angle[4] is None
    for profile in generate.POLICIES:
        text = generate.instruction(profile, angle[3])
        assert "burden" in text and "bridge" in text and "fatal" in text

def test_the_v2_angles_cover_the_weak_spots():
    cells = {(p, a) for p, _, a, _, _ in generate.plan(1.0)}
    for profile in generate.POLICIES:
        for angle in ("general_rules", "command", "means_statement", "abuse_embedded"):
            assert (profile, angle) in cells, (profile, angle)
    assert ("clinic-assistant", "record_amounts") in cells and ("adhd-assistant", "record_amounts") not in cells
    assert generate.LANGUAGES.count("English") / len(generate.LANGUAGES) <= 0.6


def test_the_v3_angles_and_the_other_language_angle():
    cells = {(p, a) for p, _, a, _, _ in generate.plan(1.0)}
    for profile in generate.POLICIES:
        for angle in ("judgment", "goodbye_signs", "other_language"):
            assert (profile, angle) in cells, (profile, angle)
    msgs = generate.prompt("clinic-assistant", "crisis", "x", 5, random.Random(1),
                           generate.LANGUAGES_OF["other_language"])
    assert "Language: English" not in msgs[1]["content"]
    assert "veterans-services" in generate.UNSEEN


def test_the_v4_angles_target_the_false_alarms():
    cells = {(p, a) for p, _, a, _, _ in generate.plan(1.0)}
    for profile in generate.POLICIES:
        for angle in ("misplacing", "overwhelm", "possessions", "plan_end"):
            assert (profile, angle) in cells, (profile, angle)
    assert ("adhd-assistant", "medication_routine") in cells and ("clinic-assistant", "medication_routine") not in cells
    assert ("clinic-assistant", "short_report") in cells and ("adhd-assistant", "short_report") not in cells
    labels = {a: lab for lab, a, *_ in generate.ANGLES}
    assert {labels[a] for a in ("misplacing", "overwhelm", "possessions", "medication_routine")} == {"allow"}
    assert labels["plan_end"] == labels["short_report"] == "crisis"
    assert "losing or misplacing things" in generate.LABEL_RULES

def test_the_v6_angles_target_what_the_crisis_benchmark_found():
    """Benchmark v1 (2026-10-22): dark topics, definitions, small talk, rudeness and role-play read as crisis;
    every eating-disorder message missed."""
    cells = {(p, a) for p, _, a, _, _ in generate.plan(1.0)}
    labels = {a: lab for lab, a, *_ in generate.ANGLES}
    allow = ("dark_topic", "definitions", "small_talk", "rude", "fiction", "procedure")
    crisis = ("eating_disorder", "eating_disorder_request")
    for profile in generate.POLICIES:
        for angle in allow + crisis:
            assert (profile, angle) in cells, (profile, angle)
    assert {labels[a] for a in allow} == {"allow"} and {labels[a] for a in crisis} == {"crisis"}
    assert "Eating-disorder risk is self-harm" in generate.LABEL_RULES
    assert "dark TOPIC" in generate.LABEL_RULES
    assert clean.RISK_WORDS.search("she makes herself throw up after every meal")


def test_examples_in_the_prompts_are_not_crisis_benchmark_messages():
    """The fix was shaped by benchmark v1, so its messages must not leak into the prompts either."""
    import difflib
    bench = [json.loads(line)["text"].lower() for line in
             (generate.TEST_SET.parent / "crisis_benchmark_v1.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    for profile, label, angle, text, _ in generate.plan(1.0):
        for example in generate.instruction(profile, text).lower().split('"')[1::2]:
            if len(example.split()) >= 4:
                best = max(difflib.SequenceMatcher(None, example, b).ratio() for b in bench)
                assert best < 0.75, (angle, example)


def test_the_v6_1_angles_restore_first_person_self_harm_requests():
    """Benchmark v1 re-run of v6 (2026-10-29): first-person self-harm requests were missed."""
    labels = {a: lab for lab, a, *_ in generate.ANGLES}
    cells = {(p, a) for p, _, a, _, _ in generate.plan(1.0)}
    for profile in generate.POLICIES:
        for angle in ("self_harm_request", "self_harm_writing"):
            assert (profile, angle) in cells and labels[angle] == "crisis"
    assert "WRITER'S OWN self-harm is crisis" in generate.LABEL_RULES
