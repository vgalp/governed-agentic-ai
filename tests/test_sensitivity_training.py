"""The sensitive-category classifier's data and training arithmetic (ADR 038 step 3). No model is called:
the frontier models are fakes, and the trainer's torch part is not run here."""

import json

import pytest

from modelops import config
from modelops import sensitivity_data as data
from modelops import sensitivity_train as training
from privacy import sensitivity
from privacy.sensitivity_model import ClassifierReader, readable_categories, rescale

SETS = sensitivity.load()
KEYS = list(readable_categories(SETS))


class FakeFrontier:
    """Answers each request with `reply(prompt)`; records what it was asked."""

    def __init__(self, name, reply):
        self.model, self._reply, self.asked = {"name": name}, reply, []

    def many(self, requests, what="requests"):
        self.asked += requests
        return [self._reply(messages) for messages, _, _ in requests]


def numbered(prompt: str) -> list[str]:
    return [line.split(". ", 1)[1] for line in prompt.splitlines() if line[:1].isdigit() and ". " in line]


# ---- plan and prompts

def test_plan_covers_every_category_and_kind_and_is_repeatable():
    cells = data.plan(SETS, 2)
    assert cells == data.plan(SETS, 2)
    assert len({c.key() for c in cells}) == len(cells)                  # each has its own cache key
    for key in KEYS:
        for stratum in ("explicit", "implicit"):
            assert any(c.stratum == stratum and c.categories == (key,) for c in cells)
        assert any(c.stratum == "hard_negative" and c.about == key and not c.categories for c in cells)
    assert any(len(c.categories) == 2 for c in cells)
    assert sum(c.stratum == "plain" for c in cells) == 2 * len(data.PLAIN_ANGLES)
    assert "protected" not in KEYS                                      # nothing to read for a source-system label


def test_implied_request_forbids_the_usual_words_and_explicit_does_not():
    implied = data.write_messages(data.Cell("implicit", ("hiv",), data.ANGLES["implicit"][0]), SETS)[1]["content"]
    named = data.write_messages(data.Cell("explicit", ("hiv",), data.ANGLES["explicit"][0]), SETS)[1]["content"]
    assert "Do not use the name of the condition" in implied
    assert "Do not use the name of the condition" not in named
    assert SETS.categories["hiv"].description in implied


def test_two_category_request_asks_for_both_about_one_person():
    ask = data.write_messages(data.Cell("implicit", ("hiv", "violence"), data.ANGLES["implicit"][0]), SETS)[1]["content"]
    assert "all of them about the same person" in ask


def test_look_alike_request_names_the_category_it_resembles():
    cell = data.Cell("hard_negative", (), data.ANGLES["hard_negative"][1], about="mental_health")
    ask = data.write_messages(cell, SETS)[1]["content"]
    assert SETS.categories["mental_health"].description in ask and "NOT in it" in ask


def test_labeller_is_not_told_what_the_texts_were_written_for():
    cell = data.Cell("implicit", ("genetic",), data.ANGLES["implicit"][2])
    system, user = data.label_messages(["One.", "Two."], SETS)
    assert "1. One." in user["content"] and "2. Two." in user["content"]
    assert cell.angle not in system["content"] + user["content"]
    assert all(k in system["content"] for k in KEYS)
    assert "{" not in system["content"]                                 # the single-text reply format is cut off


# ---- reading replies

def test_read_texts_takes_strings_and_tidies_spaces():
    assert data.read_texts('```json\n{"texts": ["a  b\\n c", 3, ""]}\n```') == ["a b c"]
    assert data.read_texts("sorry, no") == []
    assert data.read_texts('{"texts": "one"}') == []


def test_read_labels_keeps_only_entries_it_can_trust():
    reply = json.dumps({"labels": [{"n": 1, "categories": ["hiv", "hiv"]}, {"n": "2.", "categories": []},
                                   {"n": 3, "categories": ["made_up"]}, {"n": 9, "categories": []},
                                   {"n": 4, "categories": "hiv"}, "junk"]})
    assert data.read_labels(reply, 5, set(KEYS)) == {1: ("hiv",), 2: ()}
    assert data.read_labels("not json", 5, set(KEYS)) == {}


# ---- build

def writer_reply(messages):
    ask = messages[1]["content"]
    if "sexually transmitted infections, reproductive health" in ask:   # an everyday batch
        texts = [f"Please move Mr Okafor's flu shot to Thursday at {h} for the clinic." for h in (9, 10)]
    elif "NOT in it" in ask:
        texts = ["The ECG showed a depressed ST segment in lead two today.", "He says he is addicted to crosswords lately."]
    elif "HIV" in ask or "hiv" in ask.lower() and "immunodeficiency" in ask.lower():
        texts = ["His counts have been good since 2019 on the one pill a day.", "Patient is HIV positive, on treatment."]
    else:
        texts = ["She finishes a fifth of vodka most nights, her husband says.", "bad text the labeller will not agree on"]
    return json.dumps({"texts": texts})


def labeller_for(category_of):
    def reply(messages):
        texts = numbered(messages[1]["content"])
        return json.dumps({"labels": [{"n": i, "categories": category_of(t)} for i, t in enumerate(texts, 1)]})
    return reply


def category_of(text):
    if "counts" in text or "HIV" in text:
        return ["hiv"]
    if "vodka" in text:
        return ["substance_use"]
    return ["mental_health"] if "labeller will not agree" in text else []


CELLS = [data.Cell("implicit", ("hiv",), "a"), data.Cell("implicit", ("substance_use",), "a"),
         data.Cell("hard_negative", (), "a", about="mental_health"), data.Cell("plain", (), "scheduling")]


def test_build_keeps_what_both_agree_on_and_sorts_by_what_the_lists_catch():
    writer, labeller = FakeFrontier("writer-x", writer_reply), FakeFrontier("labeller-y", labeller_for(category_of))
    rows, report = data.build(writer, labeller, SETS, CELLS, "t", held=[])
    by_text = {r["text"]: r for r in rows}
    assert by_text["His counts have been good since 2019 on the one pill a day."]["stratum"] == "implicit"
    assert by_text["Patient is HIV positive, on treatment."]["stratum"] == "explicit"       # the lists catch it
    assert "bad text the labeller will not agree on" not in by_text
    assert report["writer_and_labeller_disagreed"] == {"implicit": 1}
    assert report["texts_written"] == 8 and report["kept"] == 7 == len(rows)
    assert {r["writer"] for r in rows} == {"writer-x"} and {r["labeller"] for r in rows} == {"labeller-y"}
    assert len({r["id"] for r in rows}) == len(rows)
    assert all(r["kind"] == "text" and (r["stratum"] in ("explicit", "implicit")) == bool(r["categories"]) for r in rows)
    assert [t for _, t, _ in writer.asked] == [1.0] * 4 and [t for _, t, _ in labeller.asked] == [0] * 4


def test_build_counts_a_batch_it_cannot_read_and_a_text_the_labeller_skipped():
    writer = FakeFrontier("w", lambda m: "I cannot help with that.")
    rows, report = data.build(writer, FakeFrontier("l", lambda m: "{}"), SETS, CELLS[:1], "t", held=[])
    assert rows == [] and report["batches_unreadable"] == 1 and report["texts_written"] == 0
    rows, report = data.build(FakeFrontier("w", writer_reply), FakeFrontier("l", lambda m: '{"labels": []}'), SETS,
                              CELLS[:1], "t", held=[])
    assert rows == [] and report["writer_and_labeller_disagreed"] == {"implicit": 2}


def test_cleaning_drops_duplicates_contact_details_short_text_and_text_near_the_held_out_set():
    row = lambda text: {"text": text, "categories": [], "stratum": "plain"}      # noqa: E731
    rows = [row("Please move the flu shot for room four to Thursday morning."),
            row("please move the flu shot for room four to Thursday morning"),
            row("Call her on 555-201-7788 about the flu shot appointment on Thursday."),
            row("Too short"),
            row("He finishes a fifth of vodka most nights, the wife says."),
            row("The waiting room printer needs a new toner cartridge again.")]
    kept, dropped = data.clean_rows(rows, held=["He finishes a fifth of vodka most nights."])
    assert [r["text"] for r in kept] == [rows[0]["text"], rows[5]["text"]]
    assert dropped == {"duplicate": 1, "looks like real contact details": 1, "too short or too long": 1,
                       "too close to the development set": 1}


def test_split_gives_both_parts_every_kind_and_loses_nothing():
    rows = [{"text": f"{s} {c} {i}", "categories": [c] if c else [], "stratum": s, "about": ""}
            for s, c in (("explicit", "hiv"), ("implicit", "hiv"), ("plain", "")) for i in range(20)]
    train, dev = data.split(rows, 0.1)
    assert len(train) + len(dev) == 60 and len(dev) == 6
    assert {r["stratum"] for r in dev} == {"explicit", "implicit", "plain"}
    assert data.split(rows, 0.1) == (train, dev)
    lone = [{"text": "only one", "categories": [], "stratum": "plain", "about": ""}]
    assert data.split(lone, 0.5) == (lone, [])


def test_jsonl_round_trip(tmp_path):
    rows = [{"text": "café", "categories": ["hiv"]}]
    data.write_jsonl(tmp_path / "a" / "x.jsonl", rows)
    assert data.read_jsonl(tmp_path / "a" / "x.jsonl") == rows


def test_generated_rows_are_readable_by_the_scorer(tmp_path):
    from eval.run_sensitivity import read_rows
    rows, _ = data.build(FakeFrontier("w", writer_reply), FakeFrontier("l", labeller_for(category_of)), SETS, CELLS,
                         "t", held=[])
    data.write_jsonl(tmp_path / "test.jsonl", rows)
    assert len(read_rows(tmp_path / "test.jsonl", set(SETS.categories))) == len(rows)


# ---- training arithmetic

def test_label_vector_and_unknown_category():
    assert training.label_vector(["b"], ["a", "b"]) == [0.0, 1.0]
    with pytest.raises(ValueError, match="does not learn"):
        training.label_vector(["z"], ["a", "b"])


def test_positive_weights_are_capped_and_never_below_one():
    rows = [{"categories": ["a"]}] + [{"categories": ["b"]}] * 6 + [{"categories": []}] * 93
    assert training.positive_weights(rows, ["a", "b"]) == [training.MAX_POSITIVE_WEIGHT, training.MAX_POSITIVE_WEIGHT]
    assert training.positive_weights([{"categories": ["a"]}] * 3 + [{"categories": []}], ["a"]) == [1.0]
    with pytest.raises(ValueError, match="no training row"):
        training.positive_weights(rows, ["c"])


def test_thresholds_take_the_best_f1_within_the_false_mark_limit():
    # one category; positives score 0.6 and 0.9, negatives 0.1 (x8), 0.7 and 0.3
    probabilities = [[0.6], [0.9]] + [[0.1]] * 8 + [[0.7], [0.3]]
    gold = [[1.0], [1.0]] + [[0.0]] * 10
    loose = training.choose_thresholds(probabilities, gold, ["a"], max_false_mark_rate=0.5)
    assert 0.3 < loose["a"] <= 0.6                       # catches both; one false mark allowed
    assert abs(loose["a"] - 0.5) <= 0.1                  # between equal F1, nearest 0.5
    strict = training.choose_thresholds(probabilities, gold, ["a"], max_false_mark_rate=0.0)
    assert 0.7 < strict["a"] <= 0.9                      # gives up a positive to mark no negative
    none_fit = training.choose_thresholds([[0.99], [0.99]], [[1.0], [0.0]], ["a"], max_false_mark_rate=0.0)
    assert none_fit["a"] == training.GRID[-1]


def test_measure_reports_implied_wording_and_false_marks():
    rows = [{"categories": ["a"], "stratum": "explicit"}, {"categories": ["a"], "stratum": "implicit"},
            {"categories": ["a", "b"], "stratum": "implicit"}, {"categories": [], "stratum": "hard_negative"},
            {"categories": [], "stratum": "plain"}]
    probabilities = [[0.9, 0.1], [0.2, 0.1], [0.9, 0.9], [0.1, 0.8], [0.1, 0.1]]
    m = training.measure(rows, probabilities, ["a", "b"])
    assert m["a"] == {"precision": 1.0, "recall": 0.6667, "f1": 0.8, "rows": 3}
    assert m["b"]["precision"] == 0.5 and m["b"]["recall"] == 1.0
    assert m["implied_caught"] == 0.5 and m["marked_without_any"] == 0.5
    assert training.measure(rows, probabilities, ["a", "b"], {"a": 0.15, "b": 0.85})["implied_caught"] == 1.0


def test_better_prefers_f1_then_fewer_false_marks():
    a = {"macro_f1": 0.9, "marked_without_any": 0.1}
    assert training.better(a, None)
    assert training.better({"macro_f1": 0.91, "marked_without_any": 0.3}, a)
    assert training.better({"macro_f1": 0.9, "marked_without_any": 0.05}, a)
    assert not training.better(dict(a), a)


# ---- the trained classifier as the detector's text model

def test_rescale_puts_the_threshold_at_one_half_and_keeps_order():
    assert rescale(0.2, 0.2) == 0.5 and rescale(0.0, 0.2) == 0.0 and rescale(1.0, 0.2) == 1.0
    assert rescale(0.1, 0.2) < 0.5 < rescale(0.3, 0.2)
    assert rescale(0.79, 0.8) < 0.5 <= rescale(0.8, 0.8)


def test_classifier_reader_marks_by_each_category_threshold():
    ticks = iter(range(100))
    reader = ClassifierReader(lambda text: [0.3, 0.3], ["hiv", "genetic"], {"hiv": 0.25, "genetic": 0.6}, SETS,
                              clock=lambda: next(ticks))
    found = sensitivity.Detector(SETS, model=reader, lists=False).of_text("anything")
    assert sensitivity.categories(found) == ("hiv",)
    assert reader.calls == 1 and reader.seconds == [1] and reader.unreadable == 0


def test_classifier_reader_refuses_what_it_cannot_use():
    with pytest.raises(ValueError, match="value sets do not"):
        ClassifierReader(lambda t: [0.1], ["astrology"], {"astrology": 0.5}, SETS)
    with pytest.raises(ValueError, match="threshold"):
        ClassifierReader(lambda t: [0.1], ["hiv"], {}, SETS)
    with pytest.raises(ValueError, match="different number"):
        ClassifierReader(lambda t: [0.1, 0.2], ["hiv"], {"hiv": 0.5}, SETS)("x")


# ---- settings

def write_config(tmp_path, block: str):
    text = config.DEFAULT.read_text(encoding="utf-8").split("\nsensitivity:")[0].split("# The sensitive-category")[0]
    path = tmp_path / "loop.yaml"
    path.write_text(text + block, encoding="utf-8")
    return path


def test_settings_are_optional_and_bounded(tmp_path):
    assert 1 <= config.load().sensitivity.train_batches_per_cell <= 50      # whatever loop.yaml says today
    assert config.load(write_config(tmp_path, "")).sensitivity == config.SensitivitySettings()
    custom = config.load(write_config(tmp_path, "sensitivity: {train_batches_per_cell: 2, train_args: {epochs: 2}}\n"))
    assert custom.sensitivity.train_batches_per_cell == 2 and custom.sensitivity.train_args == {"epochs": 2}
    for bad in ("{train_batches_per_cell: 5000}", "{max_false_mark_rate: 0.9}", "{batches: 3}",
                "{train_args: {epochs: 400}}", "{train_args: {optimizer: sgd}}"):
        with pytest.raises(config.ConfigError):
            config.load(write_config(tmp_path, f"sensitivity: {bad}\n"))


def test_data_command_needs_a_second_model(tmp_path, monkeypatch, capsys):
    import modelops.__main__ as cli
    monkeypatch.setattr(cli, "Ledger", lambda run: type("L", (), {"dir": tmp_path, "run_id": run})())
    monkeypatch.setattr(cli, "_frontier", lambda cfg, ledger: "one model")
    monkeypatch.setattr(cli, "_reviewer", lambda cfg, ledger, frontier: frontier)
    args = type("A", (), {"run": "t1", "action": "data"})()
    assert cli.cmd_sensitivity(args, config.load()) == 2
    assert "Add a reviewer" in capsys.readouterr().out


def test_data_command_swaps_writer_and_labeller_for_the_test_set(tmp_path, monkeypatch):
    import modelops.__main__ as cli
    cfg = config.load(write_config(tmp_path, "sensitivity: {train_batches_per_cell: 1}\n"))
    opus, sonnet = FakeFrontier("opus", writer_reply), FakeFrontier("sonnet", writer_reply)
    opus._reply = sonnet._reply = lambda m: (labeller_for(category_of)(m) if "Judge each numbered" in m[0]["content"]
                                             else writer_reply(m))
    ledger = type("L", (), {"dir": tmp_path / "run", "run_id": "t1"})()
    report = cli.sensitivity_data(cfg, ledger, opus, sonnet)
    out = tmp_path / "run" / "sensitivity"
    test, train, dev = (data.read_jsonl(out / f"{n}.jsonl") for n in ("test", "train", "dev"))
    assert {r["writer"] for r in test} == {"sonnet"} and {r["labeller"] for r in test} == {"opus"}
    assert {r["writer"] for r in train + dev} <= {"opus"}
    assert report["test"]["writer"] == "sonnet" and report["train"]["labeller"] == "sonnet"
    assert not {r["text"] for r in test} & {r["text"] for r in train + dev}        # training stays off the test set
    assert json.loads((out / "data_report.json").read_text())["value_sets_sha256"] == SETS.sha256


# ---- a test set from text people wrote

from modelops import sensitivity_mine as mine  # noqa: E402


def source_rows(n: int = 60) -> list[dict]:
    plain = ("The patient presented with fever and a productive cough for three days. Chest radiography showed a "
             "left lower lobe infiltrate. He was treated with oral antibiotics and recovered fully. Follow-up at six "
             "weeks was unremarkable in every respect.")
    cued = ("A {i}-year-old woman had a miscarriage at eleven weeks during the admission number {i}. She had "
            "lost the baby she and her husband wanted, before case {i}. Her blood pressure was normal on "
            "arrival to ward {i}. The remainder of the examination was unremarkable for patient {i}.")
    return ([{"_id": f"plain-{i}", "description": plain.replace("three", str(i + 3)).replace("six", str(i + 6))}
             for i in range(n)] + [{"_id": f"cued-{i}", "description": cued.format(i=i + 20)} for i in range(n)])


def test_sentences_are_split_and_kept_within_length():
    found = mine.sentences("Too short. This one is long enough to be kept as a sentence.  And so is this\none, with a break.")
    assert found == ["This one is long enough to be kept as a sentence.", "And so is this one, with a break."]
    assert mine.sentences("") == []


def test_choose_is_repeatable_never_repeats_and_fills_each_share():
    rows = source_rows()
    items = mine.choose(rows, n_random=10, per_category=12, categories=["reproductive_health"])
    assert items == mine.choose(rows, 10, 12, ["reproductive_health"])
    assert len({i["text"] for i in items}) == len(items)
    assert sum(i["picked"] == "random" for i in items) >= 10
    enriched = [i for i in items if i["picked"] == "enriched"]
    assert len(enriched) >= 12 and {i["cue_for"] for i in enriched} == {"reproductive_health"}
    assert {i["has_cue"] for i in enriched} == {True, False}                 # with the cue word and without
    assert all(i["source_id"].startswith("cued-") for i in enriched)
    random_ids = {i["source_id"] for i in items if i["picked"] == "random"}
    assert not random_ids & {i["source_id"] for i in enriched}               # a description is used once


def test_read_source_refuses_other_shapes(tmp_path):
    good = tmp_path / "s.jsonl"
    data.write_jsonl(good, source_rows(2))
    assert len(mine.read_source(good)) == 4
    for content in ('{"_id": "a"}\n', "not json\n", ""):
        bad = tmp_path / "bad.jsonl"
        bad.write_text(content)
        with pytest.raises(mine.SourceError):
            mine.read_source(bad)


def test_estimate_grows_with_the_sentences_and_uses_both_prices():
    items = [{"text": "one two three four five six seven"}] * 30
    models = [{"price": {"input_per_million": 15, "output_per_million": 75}},
              {"price": {"input_per_million": 3, "output_per_million": 15}}]
    assert 0 < mine.estimate_usd(items, models[1:]) < mine.estimate_usd(items, models) < mine.estimate_usd(items * 2, models)


def human_label(text):
    return ["reproductive_health"] if "miscarriage" in text or "lost the baby" in text else []


def test_label_keeps_agreement_files_by_the_lists_and_sets_the_rest_aside():
    items = mine.choose(source_rows(), 6, 8, ["reproductive_health"])
    opus = FakeFrontier("opus", labeller_for(human_label))
    doubter = FakeFrontier("sonnet", labeller_for(lambda t: [] if "lost the baby" in t else human_label(t)))
    rows, disputed, report = mine.label(opus, doubter, SETS, items, "t")
    assert report["agreed"] == len(rows) and report["not_agreed"] == len(disputed) > 0
    assert all("lost the baby" in d["text"] and d["opus"] == "reproductive_health" and d["sonnet"] == "none"
               for d in disputed)
    strata = {r["stratum"] for r in rows}
    assert "explicit" in strata and "plain" in strata and "hard_negative" in strata
    assert all((r["stratum"] in ("explicit", "implicit")) == bool(r["categories"]) for r in rows)
    assert all(r["stratum"] == "plain" for r in rows if r["picked"] == "random")
    assert len({r["id"] for r in rows}) == len(rows)
    both, _, _ = mine.label(opus, FakeFrontier("sonnet", labeller_for(human_label)), SETS, items, "t")
    assert any(r["stratum"] == "implicit" for r in both)                     # the lists do not know "lost the baby"
    assert [t for _, t, _ in opus.asked][:1] == [0]


def test_manifest_holds_no_text(tmp_path):
    source = tmp_path / "s.jsonl"
    data.write_jsonl(source, source_rows(3))
    rows = [{"id": "a#1", "text": "A secret sentence from the source.", "categories": [], "stratum": "plain", "picked": "random"}]
    m = mine.manifest(rows, source, {"agreed": 1})
    assert "secret sentence" not in json.dumps(m)
    assert m["rows"][0]["id"] == "a#1" and len(m["rows"][0]["text_sha256"]) == 64 and len(m["source_sha256"]) == 64


def mine_args(source, go):
    return type("A", (), {"run": "t1", "action": "mine", "source": str(source), "go": go, "random": 6, "per_category": 8})()


def test_mine_command_shows_the_cost_first_and_keeps_text_out_of_the_run(tmp_path, capsys):
    import modelops.__main__ as cli
    source = tmp_path / "outside" / "s.jsonl"
    data.write_jsonl(source, source_rows())
    ledger = type("L", (), {"dir": cli.ROOT / "modelops" / "runs" / "zz_test_mine", "run_id": "t1"})()
    cfg = config.load()
    try:
        assert cli.sensitivity_mine(mine_args(source, go=False), cfg, ledger) == 0
        assert "Nothing was sent" in capsys.readouterr().out and not (tmp_path / "outside" / "sensitivity_human_test_v1.jsonl").exists()
        pair = (FakeFrontier("opus", labeller_for(human_label)), FakeFrontier("sonnet", labeller_for(human_label)))
        assert cli.sensitivity_mine(mine_args(source, go=True), cfg, ledger, frontiers=pair) == 0
        kept = data.read_jsonl(tmp_path / "outside" / "sensitivity_human_test_v1.jsonl")
        manifest = json.loads((ledger.dir / "sensitivity" / "human_test_v1_manifest.json").read_text())
        assert len(kept) == len(manifest["rows"]) > 0 and kept[0]["text"] not in json.dumps(manifest)
        from eval.run_sensitivity import read_rows
        assert len(read_rows(tmp_path / "outside" / "sensitivity_human_test_v1.jsonl", set(SETS.categories))) == len(kept)
    finally:
        import shutil
        shutil.rmtree(ledger.dir, ignore_errors=True)


def test_mine_command_refuses_a_source_inside_the_repository(capsys):
    import modelops.__main__ as cli
    inside = cli.ROOT / "eval" / "datasets" / "sensitivity_dev_v1.jsonl"
    assert cli.sensitivity_mine(mine_args(inside, go=False), config.load(), None) == 2
    assert "outside this repository" in capsys.readouterr().out


def test_result_names_for_each_kind_of_dataset():
    from pathlib import Path

    from eval.run_sensitivity import DEV, result_stem
    assert result_stem(DEV) == "sensitivity_dev_v1"
    assert result_stem(Path("modelops/runs/sens1/sensitivity/test.jsonl")) == "sensitivity_sens1_test"
    assert result_stem(Path("/x/sensitivity_human_test_v1.jsonl")) == "sensitivity_human_test_v1"
    assert result_stem(Path("/x/other.jsonl")) == "sensitivity_other"
