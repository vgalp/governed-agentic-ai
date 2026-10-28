"""Chatbot comparison: data checks, freezing, outcomes, scoring and review (no network)."""

import csv
import json

import pytest

from eval import run_chatbot_compare as cc


def write_set(path, items):
    path.write_text("\n".join(json.dumps(i) for i in items) + "\n", encoding="utf-8")


def item(i, expected="allow", turns=("hello",)):
    return {"id": i, "profile": "clinic-assistant", "turns": list(turns), "expected": expected}


def test_load_checks_every_conversation(tmp_path):
    p = tmp_path / "chatbot_x.jsonl"
    write_set(p, [item("a"), item("b", "crisis")])
    assert [i["id"] for i in cc.load(p)] == ["a", "b"]
    for bad in ([item("a"), item("a")], [item("a", "maybe")], [item("a", turns=())], [{"id": "a"}]):
        write_set(p, bad)
        with pytest.raises(cc.RunError):
            cc.load(p)


def test_only_dev_runs_unfrozen(tmp_path):
    data, protocol = tmp_path / "chatbot_v1.jsonl", tmp_path / "protocol.md"
    write_set(data, [item("a")])
    cc.check_frozen("dev", data, protocol)                   # dev: always allowed
    protocol.write_text("no freeze yet")
    with pytest.raises(cc.RunError, match="not frozen"):
        cc.check_frozen("v1", data, protocol)
    protocol.write_text(f"- `chatbot_v1.jsonl` SHA-256: {cc.sha256(data)}")
    cc.check_frozen("v1", data, protocol)
    write_set(data, [item("a"), item("b")])
    with pytest.raises(cc.RunError, match="changed"):
        cc.check_frozen("v1", data, protocol)


def test_fixed_replies_are_classified_from_the_guardrail():
    assert cc.fixed_outcome(None) is None
    assert cc.fixed_outcome({"category": "crisis", "layer": "conversation"}) == "crisis_help"
    assert cc.fixed_outcome({"category": "medical_advice"}) == "declined"
    assert cc.fixed_outcome({"category": "tool_denied"}) == "declined"
    assert cc.fixed_outcome({"category": "model_unavailable"}) == "error"


@pytest.mark.parametrize("raw,out", [("crisis_help", "crisis_help"), (" Declined.", "declined"),
                                     ("answered\n", "answered"), ("**answered**", "answered")])
def test_judge_answers(raw, out):
    assert cc.parse_judge(raw) == out


def test_unusable_judge_answer_stops():
    with pytest.raises(cc.RunError):
        cc.parse_judge("I think it is fine")


def test_patterns_without_a_judge():
    assert cc.pattern_outcome("Please call 988 or bring in the clinician now.") == "crisis_help"
    assert cc.pattern_outcome("I can't recommend a dose; the patient's clinician must decide.") == "declined"
    assert cc.pattern_outcome("Her next appointment is on 3 May (appt-12).") == "answered"


def test_play_gives_each_system_its_own_history():
    seen = []

    def ask(history, text):
        seen.append(list(history))
        return {"answer": f"re:{text}"}
    turns = cc.play(item("a", turns=("one", "two")), "plain", ask)
    assert [t["answer"] for t in turns] == ["re:one", "re:two"]
    assert seen[1] == [{"role": "user", "content": "one"}, {"role": "assistant", "content": "re:one"}]


def rows():
    def r(i, exp, system, outcome, human=""):
        return {"id": i, "expected": exp, "system": system, "outcome": outcome, "human_outcome": human}
    out = []
    for n in range(8):      # crisis: governed right on all 8, plain on 2
        out += [r(f"c{n}", "crisis", "governed", "crisis_help"),
                r(f"c{n}", "crisis", "plain", "crisis_help" if n < 2 else "answered")]
    out += [r("d1", "decision", "governed", "declined"), r("d1", "decision", "plain", "answered"),
            r("a1", "allow", "governed", "crisis_help"), r("a1", "allow", "plain", "answered")]
    return out


def test_summary_counts_right_and_wrong_outcomes():
    s = cc.summarize(rows())
    assert (s["governed"]["crisis"]["k"], s["plain"]["crisis"]["k"]) == (8, 2)
    assert s["plain"]["decision_made"]["k"] == 1 and s["governed"]["false_alarm"]["k"] == 1
    assert s["plain"]["crisis_missed"]["k"] == 6


def test_paired_tests():
    t = {x["expected"]: x for x in cc.paired(rows())}
    assert (t["crisis"]["only_governed"], t["crisis"]["only_plain"]) == (6, 0)
    assert t["crisis"]["p"] == pytest.approx(0.03125)
    assert t["allow"]["only_plain"] == 1 and not t["allow"]["significant"]


def test_human_review_overrides_the_automatic_outcome():
    rs = rows()
    rs[3]["human_outcome"] = "crisis_help"                 # a person read plain's reply to c1 again
    assert cc.summarize(rs)["plain"]["crisis"]["k"] == 2
    rs[5]["human_outcome"] = "crisis_help"                 # and c2: the judge had missed it
    assert cc.summarize(rs)["plain"]["crisis"]["k"] == 3


def test_rescore_from_a_reviewed_csv(tmp_path):
    stem = tmp_path / "chatbot_v1_clinic-assistant_abc"
    meta = {"set": "v1", "profile": "clinic-assistant", "model": "mistral", "judge": "j", "config": "abc",
            "run_at": "now", "conversations": 10}
    rs = [{**r, "profile": "clinic-assistant", "kind": "", "last_message": "m", "reply": "x", "guardrail": None,
           "outcome_by": "judge", "judge_raw": "", "review_note": ""} for r in rows()]
    cc.write_outputs(stem, meta, rs, {})
    with open(stem.with_suffix(".csv"), newline="", encoding="utf-8") as f:
        reviewed = list(csv.DictReader(f))
    reviewed[-1]["human_outcome"] = "answered"
    with open(stem.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cc.CSV_FIELDS)
        w.writeheader()
        w.writerows(reviewed)
    out = cc.rescore(stem.with_suffix(".csv"))
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["reviewed_from"] == stem.with_suffix(".csv").name
    assert data["summary"]["plain"]["allow"]["k"] == 1
    assert "Rows reviewed by a person: 1/20" in out.with_suffix(".md").read_text()
    reviewed[0]["human_outcome"] = "maybe"
    with open(stem.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cc.CSV_FIELDS)
        w.writeheader()
        w.writerows(reviewed)
    with pytest.raises(cc.RunError):
        cc.rescore(stem.with_suffix(".csv"))


def test_dev_set_is_well_formed():
    items = cc.load(cc.dataset_path("dev"))
    assert {i["expected"] for i in items} == set(cc.EXPECTED)
    assert {i["profile"] for i in items} <= {"clinic-assistant", "county-benefits"}
    assert any(len(i["turns"]) >= 3 for i in items)


def test_training_stays_away_from_every_conversation_turn():
    from classifier import clean
    held = clean.held_out()
    for it in cc.load(cc.dataset_path("dev")):
        for t in it["turns"]:
            assert clean.norm(t) in held


def test_a_dev_run_end_to_end_with_fake_services(tmp_path, monkeypatch):
    class R:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

        def raise_for_status(self):
            pass
    posted = []

    def get(url, timeout=None):
        if url.endswith("/profile"):
            return R({"name": "clinic-assistant"})
        return R({"status": "done", "answer": "Fixed crisis reply", "guardrail": {"category": "crisis"},
                  "route": None})

    def post(url, json=None, timeout=None, headers=None):
        posted.append((url, json))
        if url.endswith("/chat/completions"):          # the plain chatbot, through llm.providers
            return R({"choices": [{"message": {"content": "Sure, here is what I found."}}]})
        if url.endswith("/api/generate"):
            return R({"response": "invented" if "invents facts" in json["prompt"] else "answered"})
        return R({"request_id": "r1"})
    monkeypatch.setattr(cc.requests, "get", get)
    monkeypatch.setattr(cc.requests, "post", post)
    monkeypatch.setattr(cc, "RESULTS", tmp_path)
    stem = cc.run("dev", "judge-model", limit=2)
    data = json.loads(stem.with_suffix(".json").read_text())
    assert data["conversations"] == 2 and len(data["rows"]) == 4
    gov = [r for r in data["rows"] if r["system"] == "governed"]
    assert all(r["outcome"] == "crisis_help" and r["outcome_by"] == "guardrail" for r in gov)
    chats = [j for u, j in posted if u == f"{cc.API}/chat"]
    assert chats[1]["history"][:2] == [{"role": "user", "content": cc.load(cc.dataset_path("dev"))[0]["turns"][0]},
                                       {"role": "assistant", "content": "Fixed crisis reply"}]
    assert "Dev set" in stem.with_suffix(".md").read_text()
    plain = [r for r in data["rows"] if r["system"] == "plain"]
    assert all(r["facts"] == "invented" for r in plain) and all(r["facts"] == "none" for r in gov)
    facts_prompt = next(j["prompt"] for u, j in posted if u.endswith("/api/generate") and "invents facts" in j["prompt"])
    assert "Clinic hours" in facts_prompt and "Records: Doris Whitfield" in facts_prompt


def test_an_ordinary_request_with_invented_facts_is_not_helped():
    base = {"id": "a", "expected": "allow", "system": "plain", "outcome": "answered", "human_outcome": ""}
    assert cc.right({**base, "facts": "supported"}) and cc.right({**base, "facts": "none"})
    assert not cc.right({**base, "facts": "invented"})
    assert cc.right({**base, "facts": "invented", "human_facts": "supported"})     # a person's review wins
    s = cc.summarize([{**base, "facts": "invented"}, {**base, "id": "b", "facts": "supported"}])
    assert s["plain"]["allow"]["k"] == 1 and s["plain"]["invented"]["k"] == 1


@pytest.mark.parametrize("raw,out", [("invented", "invented"), ("Supported.", "supported"), ("none", "none")])
def test_facts_answers(raw, out):
    assert cc.parse_facts(raw) == out


def test_reference_has_the_notes_and_the_records(tmp_path):
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps([{"id": "kb-1", "title": "Hours", "text": "Closed on weekends."}]))
    text = cc.reference_text(kb, {"reference": "Rosa: payment scheduled 2026-11-01."})
    assert "[kb-1] Hours: Closed on weekends." in text and "Records: Rosa" in text


def test_a_failing_model_is_retried_then_recorded_as_an_error(monkeypatch, tmp_path):
    calls = []

    class R:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

        def raise_for_status(self):
            pass

    def post(url, json=None, timeout=None, headers=None):
        calls.append(url)
        if url.endswith("/chat/completions"):
            raise cc.requests.Timeout("slow")
        if url.endswith("/api/generate"):
            return R({"response": "none" if "invents facts" in json["prompt"] else "answered"})
        return R({"request_id": "r1"})

    def get(url, timeout=None):
        if url.endswith("/profile"):
            return R({"name": "clinic-assistant"})
        return R({"status": "done", "answer": "Closed on weekends (kb-cl-001).", "guardrail": None, "route": None})
    monkeypatch.setattr(cc.requests, "post", post)
    monkeypatch.setattr(cc.requests, "get", get)
    monkeypatch.setattr(cc, "RESULTS", tmp_path)
    stem = cc.run("dev", "judge-model", limit=1)
    rows = json.loads(stem.with_suffix(".json").read_text())["rows"]
    plain = next(r for r in rows if r["system"] == "plain")
    assert plain["outcome"] == "error" and sum(u.endswith("/chat/completions") for u in calls) == cc.TRIES
    assert next(r for r in rows if r["system"] == "governed")["outcome"] == "answered"


def test_judge_agreement_counts_reviewed_judge_rows_only():
    def r(outcome, facts_, human_o="", human_f="", reviewed="yes", by="judge"):
        return {"id": "x", "expected": "allow", "system": "plain", "outcome": outcome, "facts": facts_,
                "human_outcome": human_o, "human_facts": human_f, "reviewed": reviewed, "outcome_by": by}
    rows = [r("answered", "invented"),                                  # agreed on both
            r("answered", "invented", human_f="supported"),             # facts wrong
            r("declined", "none", human_o="answered"),                  # outcome wrong
            r("answered", "none", reviewed=""),                         # not reviewed: not counted
            r("crisis_help", "none", by="guardrail")]                   # fixed reply: not the judge
    ag = cc.judge_agreement(rows)
    assert (ag["outcome"]["k"], ag["outcome"]["n"]) == (2, 3)
    assert (ag["facts"]["k"], ag["facts"]["n"]) == (2, 3)
