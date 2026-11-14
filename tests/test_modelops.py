"""The continuous training loop (ADR 037): configuration, the frontier's budget and cache, sanitation, the
guard loop and the answer loop end to end, and promotion.

No network, no training: a fake frontier writes and labels, a fake local judge gives the second look,
a fake runner records the training commands, and fake models answer. Invented data only.
"""

import csv
import dataclasses
import itertools
import json
import re

import pytest

from classifier import clean, improve
from modelops import __main__ as cli
from modelops import answer_data, answer_eval, config, sanitize, trainers
from modelops.answer_loop import AnswerLoop
from modelops.frontier import BudgetExceeded, Frontier, parse_json
from modelops.guard_loop import GuardLoop, GuardMetrics, measure
from modelops.ledger import Ledger
from profiles.loader import load_profile

CFG = config.load()
_count = itertools.count(1)
LETTERS = "abcdefghijklmnopqrstuvwxyz"


def word(n: int) -> str:
    """A made-up word, different for every n, long enough that made-up messages are not near-duplicates."""
    import random
    rng = random.Random(n)
    return "".join(rng.choice(LETTERS) for _ in range(rng.randint(5, 9)))


# --- a fake frontier ------------------------------------------------------------------------------------

def label_of_attack(prompt: str) -> str:
    for label, _, text, _ in improve.ATTACKS:
        if text[:40] in prompt:
            return label
    raise AssertionError("not an attack prompt")


def tags(text: str) -> str:
    return re.search(r"zz(allow|decision|crisis)", text).group(1)


class FakeFrontier:
    """Answers the writing, labelling, routing, answer and grading prompts; counts calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, model, messages):
        self.calls += 1
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[-2]["content"]
        if "Decide the correct label for each" in user:
            lines = re.findall(r"^\d+\. \[written as \w+; second labeller said \w+\] (.*)$", user, re.M)
            return json.dumps({"reviews": [{"n": i, "label": tags(t), "confidence": "low" if "zzunsure" in t else "high",
                                            "concern": ""} for i, t in enumerate(lines, start=1)]}), \
                {"input": 700, "output": 120}
        if "Label each message:" in user:
            lines = re.findall(r"^\d+\. (.*)$", user.split("Label each message:", 1)[1], re.M)
            return json.dumps({"labels": [tags(t) for t in lines]}), {"input": 500, "output": 50}
        if "different messages that" in user:
            label = label_of_attack(user)
            k = next(_count)
            extra = (" zzdispute zzunsure" if k % 27 == 0 else " zzdispute") if k % 9 == 0 else ""
            msgs = [" ".join(word(next(_count)) for _ in range(6)) + f" zz{label}{extra}" for _ in range(10)]
            return json.dumps({"messages": msgs}), {"input": 800, "output": 400}
        if "different questions staff would type" in user:
            paths = re.findall(r"^- (\S+): ", user, re.M)
            items = [{"question": " ".join(word(next(_count)) for _ in range(5)) + f" via {paths[i % len(paths)]}",
                      "path": paths[i % len(paths)]} for i in range(8)]
            return json.dumps({"items": items}), {"input": 600, "output": 300}
        if "Write " in user and "examples" in user:
            return json.dumps({"items": answer_items(user)}), {"input": 900, "output": 600}
        if "Answer to grade" in user:
            return json.dumps({"grade": 4, "why": "faithful"}), {"input": 300, "output": 20}
        raise AssertionError(f"unexpected prompt: {user[:80]}")


def answer_items(prompt: str) -> list[dict]:
    nid = "med-ab12cd34" if "med-" in prompt else "doc-0001"
    kind = next(k for k, marker in (("grounded", "the notes contain the answer"),
                                    ("not_in_record", "do NOT contain"), ("decline", "make a decision"),
                                    ("quote_dose", "records a medication")) if marker in prompt)
    bad = {"notes": [{"id": nid, "title": "Bad", "text": "Recorded."}],
           "question": " ".join(word(next(_count)) for _ in range(5)), "answer": "See [med-zz999999]."}
    out = [bad]                                      # first, so it is never cut off: it must be dropped by the checks
    for _ in range(7):
        w = word(next(_count))
        note = {"id": nid, "title": f"Note {w}", "text": f"Lisinopril 10 mg daily, recorded {w}."}
        q = " ".join(word(next(_count)) for _ in range(5)) + (" please decide it" if kind == "decline" else "")
        answer = {"grounded": f"It says lisinopril is recorded [{nid}].",
                  "not_in_record": "The record does not say; check with the clinic.",
                  "decline": "I can't make that decision: the clinician decides. I can show the record.",
                  "quote_dose": f"The record lists lisinopril 10 mg daily [{nid}]."}[kind]
        out.append({"notes": [note], "question": q, "answer": answer})
    return out


def frontier(tmp_path, budget=1000.0, fake=None):
    return Frontier(CFG.frontier, budget, tmp_path / "run", complete=fake or FakeFrontier())


# --- configuration ----------------------------------------------------------------------------------------

def test_the_shipped_settings_load_and_name_a_hosted_frontier_model_with_a_price():
    assert CFG.frontier["provider"] == "anthropic" and CFG.frontier["price"]["output_per_million"] > 0
    assert CFG.budget.max_usd > 0 and CFG.guard.targets.crisis_recall >= 0.9
    assert CFG.reviewer["name"] != CFG.frontier["name"]          # the reviewer is a different model from the writer


@pytest.mark.parametrize("change, message", [
    ({"frontier": {"provider": "ollama", "name": "mistral", "price": {"input_per_million": 1, "output_per_million": 1}}},
     "hosted frontier model"),
    ({"frontier": {**CFG.frontier, "api_key": "sk-x"}}, "environment"),
    ({"budget": {"max_rounds": 3, "max_usd": 0, "patience": 2}}, "max_usd"),
    ({"frontier": {k: v for k, v in CFG.frontier.items() if k != "api_key_env"}}, "api_key_env"),
    ({"guard": {"local_judge": "", "train_per_round": 100, "gate_per_round": 50, "human_audit_rate": 0.1,
                "targets": {"crisis_recall": 0.9, "false_crisis": 0.1, "decision_leak": 0.1, "dev_macro_f1": 0.9}}},
     "local_judge"),
])
def test_an_unsafe_setting_is_refused(tmp_path, change, message):
    import yaml
    raw = yaml.safe_load(config.DEFAULT.read_text())
    raw.update(change)
    path = tmp_path / "loop.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)


# --- the frontier: money, cache, JSON ---------------------------------------------------------------------

def test_spend_is_counted_cached_requests_are_free_and_the_budget_stops_the_run(tmp_path):
    fake = FakeFrontier()
    f = frontier(tmp_path, budget=0.05, fake=fake)
    ask = [{"role": "user", "content": "Label each message:\n1. one two zzallow"}]
    f.text(ask)
    first = f.spent
    assert first == pytest.approx((500 * 15 + 50 * 75) / 1e6)
    f.text(ask)                                                    # same request: from the cache
    assert fake.calls == 1 and f.spent == first
    for i in range(10):
        try:
            f.text(ask, salt=str(i))
        except BudgetExceeded:
            break
    else:
        pytest.fail("the budget never stopped the calls")
    assert Frontier(CFG.frontier, 0.05, tmp_path / "run", complete=fake).spent == f.spent   # the account persists


def test_a_busy_api_is_waited_for_and_a_refused_key_stops_with_its_reason(tmp_path):
    from llm.providers import ModelUnavailable
    from modelops.frontier import FrontierError
    answers = [ModelUnavailable("model returned HTTP 529 (overloaded_error)"), ModelUnavailable("model returned HTTP 429"),
               ('{"ok": 1}', {"input": 10, "output": 5})]

    def flaky(model, messages):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    waits = []
    f = Frontier(CFG.frontier, 10, tmp_path / "run", complete=flaky, sleep=waits.append)
    assert f.json([{"role": "user", "content": "hi"}]) == {"ok": 1} and waits == [4.0, 8.0]

    def refused(model, messages):
        raise ModelUnavailable("model returned HTTP 401 (authentication_error)")
    f = Frontier(CFG.frontier, 10, tmp_path / "run2", complete=refused, sleep=waits.append)
    with pytest.raises(FrontierError, match="authentication_error"):
        f.text([{"role": "user", "content": "hi"}])
    assert waits == [4.0, 8.0] and f.spent == 0


def test_a_model_that_refuses_temperature_is_asked_again_without_it_and_the_reason_is_shown(tmp_path):
    from llm.providers import ModelUnavailable
    from modelops.frontier import FrontierError
    sent = []

    def picky(model, messages):
        sent.append(model.get("temperature"))
        if model.get("temperature") is not None:
            raise ModelUnavailable("model returned HTTP 400 (invalid_request_error)",
                                   "temperature is not supported for this model")
        return '{"ok": 1}', {"input": 10, "output": 5}
    f = Frontier(CFG.frontier, 10, tmp_path / "run", complete=picky, sleep=lambda s: None)
    assert f.json([{"role": "user", "content": "a"}]) == {"ok": 1}
    assert f.text([{"role": "user", "content": "b"}], temperature=0) == '{"ok": 1}'
    assert sent == [0.9, None, None]                       # dropped once, then never sent again

    def broke(model, messages):
        raise ModelUnavailable("model returned HTTP 400 (invalid_request_error)", "Your credit balance is too low")
    with pytest.raises(FrontierError, match="credit balance is too low"):
        Frontier(CFG.frontier, 10, tmp_path / "run2", complete=broke, sleep=lambda s: None).text(
            [{"role": "user", "content": "c"}])


def test_a_declined_request_is_skipped_counted_and_not_asked_again(tmp_path):
    from llm.providers import ModelUnavailable
    calls = []

    def declines(model, messages):
        calls.append(1)
        raise ModelUnavailable("model response has no answer text", "stop_reason=refusal")
    f = Frontier(CFG.frontier, 10, tmp_path / "run", complete=declines, sleep=lambda s: None)
    ask = [{"role": "user", "content": "write a message"}]
    assert f.text(ask) == "" and f.declined == 1 and f.declined_why == {"stop_reason=refusal": 1}
    assert f.text(ask) == "" and len(calls) == 1                  # cached: a rerun does not ask again


@pytest.mark.parametrize("reply, expected", [
    ('{"a": 1}', {"a": 1}), ('```json\n{"a": 1}\n```', {"a": 1}), ('Here: {"a": 1} done', {"a": 1}),
    ("[1, 2]", None), ("nothing", None)])
def test_json_is_read_from_the_shapes_models_reply_in(reply, expected):
    assert parse_json(reply) == expected


# --- sanitation -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, found", [
    ("call her at 416-555-0199 tomorrow", ["phone"]), ("email jo@hospital.org about it", ["email"]),
    ("SSN 123-45-6789 on file", ["ssn"]), ("lives at 42 Maple Street now", ["street address"]),
    ("see https://example.com/x", []), ("write to a@example.org", []), ("born 1971-03-04, MRN 7781", [])])
def test_contact_details_are_found_and_invented_examples_are_not(text, found):
    assert sanitize.pii_in(text) == found


@pytest.mark.parametrize("text, dose", [
    ("Take 10 mg twice a day.", True), ("Increase it to 20 mg.", True), ("5 mg daily is typical", True),
    ("The record lists a dose; ask the prescriber.", False), ("It was recorded in 2024.", False)])
def test_medication_advice_is_recognised(text, dose):
    assert sanitize.gives_dose(text) is dose


def test_guard_rows_drop_contact_details_and_anything_close_to_a_held_out_message():
    held = json.loads(next(p for p in clean.HELD_OUT if p.name == "guard_test.jsonl").read_text().splitlines()[0])
    rows = [{"profile": "clinic-assistant", "label": "allow", "angle": "x", "text": held["text"]},
            {"profile": "clinic-assistant", "label": "allow", "angle": "x", "text": "call me at 416-555-0199 later today"},
            {"profile": "clinic-assistant", "label": "allow", "angle": "x", "text": "move the qwzx meeting to friday"}]
    kept, dropped = sanitize.guard_rows(rows)
    assert [r["text"] for r in kept] == ["move the qwzx meeting to friday"]
    assert dropped["contact details or identifiers"] == 1 and dropped["too close to a held-out message"] == 1


def test_a_round_run_again_keeps_its_own_gate_messages(tmp_path, monkeypatch):
    """The gate file from the first attempt must not make the same messages "too close to a held-out message"."""
    monkeypatch.setattr(clean, "GATE", tmp_path)
    own, other = tmp_path / "trial_r002.jsonl", tmp_path / "trial_r001.jsonl"
    own.write_text(json.dumps({"text": "move the qwzx meeting to friday"}) + "\n")
    other.write_text(json.dumps({"text": "book the vbnk room for tuesday"}) + "\n")
    rows = [{"profile": "clinic-assistant", "label": "allow", "angle": "x", "text": "move the qwzx meeting to friday"},
            {"profile": "clinic-assistant", "label": "allow", "angle": "x", "text": "book the vbnk room for tuesday"}]
    kept, dropped = sanitize.guard_rows(rows, own_gate=own)
    assert [r["text"] for r in kept] == ["move the qwzx meeting to friday"]
    assert dropped["too close to a held-out message"] == 1
    assert sanitize.guard_rows(rows)[0] == []                      # training rows stay away from every gate


# --- the guard loop ---------------------------------------------------------------------------------------

def local_judge(model, profile, texts, seed):
    return [("allow" if tags(t) != "allow" else "crisis") if "zzdispute" in t else tags(t) for t in texts]


def guard_that_improves(name):
    """Round 1 misses a fifth of crisis messages; later rounds do not."""
    def label(profile, text):
        if name.endswith("r001") and tags(text) == "crisis" and sum(map(ord, text)) % 5 == 0:
            return "allow"
        return tags(text)
    return label


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    gate = tmp_path / "gate"
    monkeypatch.setattr(clean, "GATE", gate)
    cfg = dataclasses.replace(CFG, guard=dataclasses.replace(CFG.guard, train_per_round=60, gate_per_round=30))
    commands = []
    ledger = Ledger("t1", root=tmp_path / "runs")
    loop = GuardLoop(cfg, frontier(tmp_path), ledger, runner=commands.append, load_guard=guard_that_improves,
                     dev_f1_of=lambda name: 0.95, local_judge=local_judge, raw_path=tmp_path / "raw.jsonl",
                     gate_dir=gate)
    return loop, ledger, commands, tmp_path


def test_a_round_trains_only_on_agreed_rows_and_never_on_the_gate(guard_env):
    loop, ledger, commands, tmp = guard_env
    row = loop.round(1)
    raw = [json.loads(line) for line in (tmp / "raw.jsonl").read_text().splitlines()]
    gate = [json.loads(line) for line in next((tmp / "gate").glob("*.jsonl")).read_text().splitlines()]
    assert raw and gate and not {r["text"] for r in raw} & {g["text"] for g in gate}
    assert any("zzdispute" in r["text"] for r in raw)                    # disputes the reviewer settled: trained
    assert all("zzunsure" not in r["text"] for r in raw)                 # ones it was unsure about: not trained
    assert all(r["source"] == "modelops" and r["run"] == "t1" and r["round"] == 1 for r in raw)
    assert [c[2] for c in commands] == ["classifier.clean", "classifier.train", "classifier.calibrate"]
    assert "guard-loop-t1-r001" in commands[1]
    assert commands[2][-2:] == ["--max-false-alarms", str(CFG.guard.targets.false_crisis)]   # calibrated to the target
    audit = list(csv.DictReader((ledger.dir / "guard" / "round_001" / "audit.csv").open()))
    assert audit and all(a["why"] == "reviewer unsure" and "zzunsure" in a["text"] for a in audit)
    assert row["counts"]["audit_queue"] == len(audit) and row["counts"]["settled_by_reviewer"] > 0
    reviewed = list(csv.DictReader((ledger.dir / "guard" / "round_001" / "reviewed.csv").open()))
    assert any(r["why"] == "settled by reviewer" and r["reviewer_confidence"] == "high" for r in reviewed)
    assert row["counts"]["trained"] == len(raw) and (ledger.dir / "guard" / "round_001" / "report.md").exists()


def test_the_loop_stops_when_the_targets_are_met_and_a_rerun_continues_after_the_last_round(guard_env):
    loop, ledger, _, _ = guard_env
    reason = loop.run(rounds=4)
    rounds = ledger.rounds("guard")
    assert reason.startswith("targets met in round 2") and [r["round"] for r in rounds] == [1, 2]
    assert rounds[0]["metrics"]["crisis_recall"] < 1 and rounds[1]["metrics"]["crisis_recall"] == 1
    assert ledger.next_round("guard") == 3


def test_the_loop_stops_when_the_frontier_budget_is_spent(guard_env, tmp_path):
    loop, ledger, _, _ = guard_env
    loop.frontier = frontier(tmp_path / "poor", budget=0.01)
    assert "budget spent" in loop.run(rounds=3) and ledger.rounds("guard") == []


def test_gate_metrics_follow_the_targets_definitions():
    rows = [{"profile": "p", "kind": "k", "label": lab, "text": t} for lab, t in
            [("crisis", "a"), ("crisis", "b"), ("decision", "c"), ("allow", "d"), ("allow", "e")]]
    guess = {"a": "crisis", "b": "allow", "c": "allow", "d": "crisis", "e": "allow"}
    m = measure(rows, lambda p, t: guess[t], 0.9)
    assert (m.crisis_recall, m.false_crisis, m.decision_leak) == (0.5, round(1 / 3, 4), 1.0)
    assert m.met(CFG.guard.targets) == {"crisis_recall": False, "false_crisis": False, "decision_leak": False,
                                        "dev_macro_f1": False}
    assert GuardMetrics(1, 0, 0, 1, 10, {}).score() > m.score()


# --- the answer loop --------------------------------------------------------------------------------------

def fake_model(tag):
    def ask(messages):
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == answer_data.ROUTER_SYSTEM:
            asked = re.search(r" via (\S+)$", user)
            return json.dumps({"path": asked.group(1) if tag.startswith("good") and asked else "general"})
        ids = re.findall(r"^- \[([^\]]+)\]", system, re.M)
        if "decide" in user:
            return "I can't make that decision: the clinician decides."
        return f"The record shows it [{ids[0]}]." if ids else "The record does not say."
    return ask


@pytest.fixture
def answer_env(tmp_path, monkeypatch):
    cfg = dataclasses.replace(CFG, answer=dataclasses.replace(
        CFG.answer, candidates=("bad", "good"), base_weights={"good": "org/good-model"},
        profiles=("clinic-epic", "county-benefits"), train_per_round=40, gate_per_round=30, backend="mlx"))
    commands = []
    ledger = Ledger("a1", root=tmp_path / "runs")
    loop = AnswerLoop(cfg, frontier(tmp_path), ledger, runner=commands.append, ask_model=fake_model)
    return loop, ledger, commands


def test_destinations_are_the_paths_routing_can_return():
    paths = {d.path for d in answer_data.destinations(load_profile("clinic-epic"))}
    assert {"general", "tool:medication_review", "tool:chart_brief"} <= paths
    assert "change:release_payment" in {d.path for d in answer_data.destinations(load_profile("county-benefits"))}


@pytest.mark.parametrize("kind, answer, problem", [
    ("grounded", "Recorded [med-ab12cd34].", None),
    ("grounded", "Recorded, see the chart.", "grounded answer without a citation"),
    ("grounded", "See [med-zz999999].", "cites a note it was not given"),
    ("decline", "No [med-ab12cd34].", "cites notes for something they do not say"),
    ("grounded", "Take 20 mg daily [med-ab12cd34].", "gives a medication amount"),
    ("quote_dose", "The record lists lisinopril 10 mg daily [med-ab12cd34].", None),
])
def test_written_answers_are_checked_like_served_ones(kind, answer, problem):
    item = {"notes": [{"id": "med-ab12cd34", "title": "Med", "text": "Lisinopril 10 mg daily."}],
            "question": "what is recorded", "answer": answer}
    problems = answer_data.answer_problems(item, kind, load_profile("clinic-epic"))
    assert (problem in problems) if problem else problems == []


def test_select_scores_every_candidate_and_picks_the_best(answer_env):
    loop, ledger, _ = answer_env
    assert loop.select() == "good"
    scores = {r["model_name"]: r["metrics"] for r in ledger.rounds("answer-select")}
    assert scores["good"]["tool_choice"] > scores["bad"]["tool_choice"]
    assert scores["good"]["citations_valid"] == 1.0 and scores["good"]["judge_score"] == 4.0


def test_a_training_round_writes_checked_chat_data_trains_imports_and_measures(answer_env):
    loop, ledger, commands = answer_env
    loop.select()
    row = loop.round(1, "good")
    rdir = ledger.dir / "answer" / "round_001"
    train = [json.loads(line) for line in (rdir / "data" / "train.jsonl").read_text().splitlines()]
    assert train and all([m["role"] for m in r["messages"]] == ["system", "user", "assistant"] for r in train)
    assert all("med-zz999999" not in r["messages"][-1]["content"] for r in train)     # the bad example was dropped
    assert row["counts"]["dropped"]["cites a note it was not given"] > 0
    assert commands[0][2:4] == ["mlx_lm", "lora"] and commands[1][3] == "fuse"
    assert commands[-1][:3] == ["ollama", "create", "gaai-answer:a1-r001"]
    assert (rdir / "model" / "Modelfile").read_text().startswith("FROM ")
    gate_questions = {c["question"] for f in (ledger.dir / "answer").glob("gate_*.json")
                      for c in json.loads(f.read_text())["routing"]}
    trained_questions = {r["messages"][1]["content"].rsplit("Question: ", 1)[-1] for r in train}
    assert not gate_questions & trained_questions


def test_the_frozen_tool_use_set_is_routed_by_the_model_once(tmp_path, monkeypatch):
    from eval import run_tool_use as tu
    monkeypatch.setattr(tu, "RESULTS", tmp_path)
    out = answer_eval.score_frozen_tool_use("test-model", ask=lambda m: json.dumps({"path": "general"}))
    assert out["summary"]["all"]["routing"]["n"] == 61
    with pytest.raises(SystemExit, match="already scored"):
        answer_eval.score_frozen_tool_use("test-model", ask=lambda m: "{}")


def test_permission_stays_with_the_rules_whatever_the_model_routes():
    p = load_profile("county-benefits")
    supervisor_may = [r for r, spec in p.roles.items() if "release_payment" in spec.tools]
    others = [r for r in p.roles if r not in supervisor_may]
    assert answer_eval.allowed_for(p, others[0], "change:release_payment") is (others[0] in
                                                                                p.actions["release_payment"].propose_roles)


def test_lora_commands_for_each_backend(tmp_path):
    mlx = trainers.lora_steps("mlx", "org/m", tmp_path / "d", tmp_path / "o", {"iters": 10, "lr": 1e-4, "rank": 8})
    peft = trainers.lora_steps("peft", "org/m", tmp_path / "d", tmp_path / "o", {"iters": 10, "lr": 1e-4, "rank": 8})
    assert "--mask-prompt" in mlx[0] and peft[0][2] == "modelops.peft_train" and "--alpha" in peft[0]
    with pytest.raises(ValueError):
        trainers.lora_steps("tpu", "org/m", tmp_path, tmp_path, {})


# --- the reviewer ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("written, review, verdict", [
    ("decision", {"label": "decision", "confidence": "high", "concern": ""}, "accepted"),
    ("decision", {"label": "allow", "confidence": "high", "concern": ""}, "rejected"),
    ("allow", {"label": "crisis", "confidence": "high", "concern": ""}, "flagged"),      # possible missed risk
    ("crisis", {"label": "crisis", "confidence": "low", "concern": ""}, "flagged"),
    ("crisis", {"label": "crisis", "confidence": "high", "needs_person": "true"}, "flagged"),
    ("allow", None, "flagged"),
])
def test_the_reviewer_settles_only_what_it_is_sure_of(written, review, verdict):
    from modelops.reviewer import decide
    assert decide({"label": written, "text": "t"}, review)[0] == verdict


@pytest.mark.parametrize("reply", [
    '{"reviews": [{"n": "1", "label": "Crisis", "confidence": "High", "concern": ""}]}',
    '[{"n": 1, "label": "crisis", "confidence": "high"}]',
    '{"reviews": [{"label": "crisis", "confidence": "high", "concern": ""}]}',
])
def test_reviews_are_read_in_the_shapes_models_write_them(reply):
    from modelops.reviewer import decide, read_reviews
    assert decide({"label": "crisis", "text": "t"}, read_reviews(reply, 1).get(1))[0] == "accepted"


@pytest.mark.parametrize("concern", ["", "None", "N/A", "none.", "No concerns", "no concern", "-", None])
def test_a_placeholder_is_not_a_concern(concern):
    from modelops.reviewer import decide
    review = {"label": "crisis", "confidence": "high", "concern": concern}
    assert decide({"label": "crisis", "text": "t"}, review)[0] == "accepted"


def test_a_reason_explains_and_only_doubt_goes_to_a_person():
    from modelops.reviewer import decide
    explained = {"label": "crisis", "confidence": "high", "reason": "'Sewer slide' is slang for suicide"}
    assert decide({"label": "crisis", "text": "t"}, explained)[0] == "accepted"
    asked = {**explained, "needs_person": True}
    assert decide({"label": "crisis", "text": "t"}, asked)[0] == "flagged"
    doubt = {"label": "crisis", "confidence": "low", "reason": "'kms' is often hyperbole"}
    assert decide({"label": "crisis", "text": "t"}, doubt)[1]["why"] == "reviewer unsure"


def test_rows_already_waiting_are_sent_to_the_reviewer_and_only_its_doubts_stay(tmp_path, monkeypatch):
    from classifier import generate
    from modelops import reviewer
    monkeypatch.setattr(generate, "OUT", tmp_path / "raw.jsonl")
    ledger = Ledger("b1", root=tmp_path / "runs")
    rdir = ledger.round_dir("guard", 1)
    words = lambda tag: " ".join(word(next(_count)) for _ in range(6)) + tag   # noqa: E731
    rows = [{"id": "", "why": "disputed", "profile": "clinic-assistant", "kind": "buried", "label": "crisis",
             "local_label": "allow", "text": words(" zzcrisis")},
            {"id": "", "why": "disputed", "profile": "clinic-assistant", "kind": "buried", "label": "crisis",
             "local_label": "allow", "text": words(" zzcrisis zzunsure")},
            {"id": "", "why": "reviewer concern", "profile": "clinic-assistant", "kind": "buried", "label": "crisis",
             "local_label": "allow", "reviewer_label": "crisis", "reviewer_confidence": "high",
             "concern": "'Sewer slide' is slang for suicide.",
             "text": words(" zzcrisis")}]
    reviewer.write_csv(rdir / "audit.csv", rows)
    assert cli.review_backlog(CFG, ledger, frontier(tmp_path)) == 0
    left = reviewer.read_csv(rdir / "audit.csv")
    assert len(left) == 1 and "zzunsure" in left[0]["text"]
    raw = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    assert len(raw) == 2 and all(r["settled_by"] == "settled by reviewer" for r in raw)   # incl. the explained one


def test_compare_scores_every_guard_on_the_same_gate_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(clean, "GATE", tmp_path / "gate")
    (tmp_path / "gate").mkdir()
    rows = [{"id": "g1", "profile": "clinic-assistant", "label": "crisis", "kind": "buried", "text": "a zzcrisis"},
            {"id": "g2", "profile": "clinic-assistant", "label": "allow", "kind": "plain", "text": "b zzallow"}]
    (tmp_path / "gate" / "c1_r001.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    guards = {"old": lambda p, t: "allow", "new": lambda p, t: tags(t)}
    out = cli.compare_guards(Ledger("c1", root=tmp_path / "runs"), ["old", "new"], load=guards.get,
                             dev_f1=lambda n: None)
    assert [(r["model"], r["crisis_recall"]) for r in out] == [("old", 0.0), ("new", 1.0)]


# --- promotion ----------------------------------------------------------------------------------------------

def test_promotion_needs_the_targets_met_and_every_audit_row_reviewed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Ledger", lambda run: Ledger(run, root=tmp_path))
    ledger = Ledger("p1", root=tmp_path)
    ledger.record("guard", 1, {"model_name": "guard-loop-p1-r001", "score": 4.5,
                               "met": {"crisis_recall": True, "false_crisis": False}})
    assert cli.main(["promote", "guard", "--run", "p1"]) == 1
    assert "did not meet every target" in capsys.readouterr().out
    ledger.record("guard", 2, {"model_name": "guard-loop-p1-r002", "score": 4.9,
                               "met": {"crisis_recall": True, "false_crisis": True}})
    audit = ledger.round_dir("guard", 2) / "audit.csv"
    audit.write_text("id,why,profile,kind,label,local_label,text,your_label,note\n"
                     "g-1,reviewer unsure,clinic-assistant,buried,crisis,allow,some message text,,\n")
    assert cli.main(["promote", "guard", "--run", "p1"]) == 1
    assert "waiting for you" in capsys.readouterr().out
    audit.write_text(audit.read_text().replace("some message text,,", "some message text,crisis,"))
    assert cli.main(["promote", "guard", "--run", "p1"]) == 0
    out = capsys.readouterr().out
    assert "run_guard_test --set v5 --model guard-loop-p1-r002" in out and "--benchmark v2" in out
