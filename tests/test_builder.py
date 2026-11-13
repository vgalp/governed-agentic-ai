"""gaai new: building an office profile from set questions (ADR 034). The profile must load, refuse
the office's decisions, hand over a crisis (also one relayed by staff) and let ordinary questions
through, with or without the local model's drafts. No network: a fake Ollama answers."""

import json

import pytest

from cli import builder as b
from guardrails.rules import check_input
from profiles.loader import load_profile_from

FAQ = """Office hours
Student services is open Monday to Friday, 8:00 to 15:30.

Absence reporting
Parents report an absence by calling 555-0142 before 9:00.
"""


def school(**kw):
    a = b.Answers(
        name="riverside-school", title="Riverside High School student services",
        office="Answers questions from parents and staff about school policies.", audience="staff",
        decisions=[b.Decision("decision_suspension", "whether a student is suspended", "the principal",
                              ["should we suspend", "suspend him", "suspend her"],
                              ["Should we suspend Jordan for fighting at lunch?"])],
        crisis_contact="The school counsellor is at 555-0100.", knowledge=[{"raw": FAQ}], web=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def build(a, drafter=None):
    b.draft(a, drafter)
    return b.generate(a)


def test_a_profile_from_answers_alone_loads_and_passes_its_checks():
    a = school()
    path = build(a)
    passed, lines = b.check(path, a)
    assert passed, lines
    p = load_profile_from(path)
    assert p.name == "riverside-school" and [k["title"] for k in a.knowledge] == ["Office hours", "Absence reporting"]
    assert p.response("decision_suspension").startswith("I can't decide whether a student is suspended")
    assert "school counsellor is at 555-0100" in p.response("crisis")
    assert p.response("crisis").startswith("Please stay with the person")        # worded for staff


@pytest.mark.parametrize("text, category", [
    ("Should we suspend her?", "decision_suspension"),
    ("My daughter said she wants to kill herself.", "crisis"),
    ("He has been cutting himself, his teacher noticed.", "crisis"),
    ("I keep thinking about ending my life.", "crisis"),
    ("How much Tylenol should I give my kid?", "medication"),           # the template's baseline stays
    ("What is the suspension policy?", None),                          # a policy question, not a decision
    ("When is the office open?", None),
])
def test_the_new_profile_refuses_hands_over_and_answers(text, category):
    p = load_profile_from(build(school()))
    got = check_input(text, p)
    assert got.category == category and got.allowed is (category is None)


def test_phrases_are_matched_as_written_never_as_patterns():
    a = school()
    a.decisions[0].phrases += ["(.*)+ grade", "send him home"]      # a model could produce anything
    p = load_profile_from(build(a))
    assert "(.*)+ grade" not in a.decisions[0].phrases           # symbols: dropped, never a pattern
    assert check_input("What grade is he in?", p).allowed
    assert check_input("Should we send him home for the day?", p).category == "decision_suspension"
    assert check_input("Should we send.him.home?", p).allowed    # a dot is a dot, not "any character"


def test_without_web_search_the_tool_and_its_judge_are_left_out():
    path = build(school())
    p = load_profile_from(path)
    assert "web_research" not in p.tools and p.faithfulness is None and not (path / "web_research.yaml").exists()
    p2 = load_profile_from(build(school(name="riverside-web", web=True)))
    assert "web_research" in p2.tools


class FakeOllama:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        body = self.replies.pop(0)

        class R:
            def json(self_inner):
                return {"message": {"content": body if isinstance(body, str) else __import__("json").dumps(body)}}
        return R()


def test_the_local_model_adds_phrases_replies_notes_and_texts():
    fake = FakeOllama([
        {"notes": [{"title": "Hours", "text": "Open 8:00 to 15:30, Monday to Friday.", "tags": ["hours"]},
                   {"title": "Absences", "text": "Call 555-0142 before 9:00.", "tags": ["absence"]}]},
        {"phrases": ["a suspension for", "send her home for three days", "(.*)"],
         "tests": ["Can we give Sam a suspension for vaping?"],
         "reply": "That decision belongs to the principal. I can explain the code of conduct."},
        {"description": "An assistant for student services.", "guard_policy": "It must never decide suspensions.",
         "examples": ["When is the office open?", "Should we suspend Sam?"]}])
    a = school()
    path = build(a, b.Drafter("qwen3:8b", "http://ollama.test", fake))
    assert "a suspension for" in a.decisions[0].phrases and "should we suspend" in a.decisions[0].phrases
    assert "(.*)" not in a.decisions[0].phrases
    assert [k["title"] for k in a.knowledge] == ["Hours", "Absences"]
    passed, lines = b.check(path, a)
    assert passed, lines
    p = load_profile_from(path)
    assert p.response("decision_suspension").startswith("That decision belongs to the principal")
    assert all(s["format"]["type"] == "object" and s["stream"] is False for s in fake.sent)
    notes = json.loads((path / "data/knowledge_base.json").read_text())
    assert [n["id"] for n in notes] == ["riv-001", "riv-002"] and notes[0]["status"].startswith("DRAFT")


def test_a_model_reply_that_is_not_json_falls_back_to_the_answers():
    a = school()
    path = build(a, b.Drafter("qwen3:8b", "http://ollama.test", FakeOllama(["not json", "{]", "nope"])))
    assert a.decisions[0].reply.startswith("I can't decide") and len(a.knowledge) == 2
    assert b.check(path, a)[0]


def test_a_decision_its_phrases_miss_is_reported():
    a = school()
    a.decisions[0].tests.append("Can Jordan be sent home for a week?")
    passed, lines = b.check(build(a), a)
    assert not passed
    assert (False, 'refused (not refused): "Can Jordan be sent home for a week?"') in lines


def test_save_will_not_overwrite_a_profile(tmp_path):
    (tmp_path / "profiles" / "riverside-school").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        b.save(build(school()), "riverside-school", root=tmp_path)


def test_the_interview_asks_the_set_questions_and_rejects_a_taken_name():
    answers = iter(["County clerk's office", "general-assistant", "", "Records and licences for the county.",
                    "public", "whether a licence is granted", "the clerk", "approve my licence", "",
                    "Will you approve my licence?", "", "", "", "Hours", "Open 9 to 5.", "END", "no"])
    said = []
    a = b.Interview(ask=lambda q: next(answers), say=said.append).run()
    assert a.name == "county-clerk-s-office" and a.audience == "public" and a.web is False
    assert a.decisions[0].phrases == ["approve my licence"] and a.decisions[0].tests == ["Will you approve my licence?"]
    assert a.knowledge == [{"raw": "Hours\nOpen 9 to 5."}]
    assert any("taken" in s for s in said)


def test_new_in_the_console_builds_checks_and_saves_only_on_yes(monkeypatch, tmp_path):
    from cli import agent as ag
    from cli import platform as pf
    from cli.shell import Shell

    class Down:                              # no API, no Ollama: the builder uses the answers alone
        def get(self, *a, **k):
            raise pf.requests.ConnectionError("down")

        def post(self, *a, **k):
            raise pf.requests.ConnectionError("down")
    saved = []
    monkeypatch.setattr(b, "save", lambda path, name, root=None: saved.append((path, name)) or tmp_path / name)
    script = ["Riverside High School student services", "riverside-test",
              "Answers questions from parents and staff about school policies.", "staff",
              "whether a student is suspended", "the principal", "should we suspend", "",
              "Should we suspend Jordan for fighting?", "", "", "", *FAQ.splitlines(), "END", "no"]
    for answer, expect in (("n", []), ("y", ["riverside-test"])):
        inputs, out = [*script, answer], []
        api = pf.Api("http://api.test", session=Down())
        agent = ag.Agent(ag.tools(api), confirm=lambda t, x: False, session=Down())
        sh = Shell(api=api, agent=agent, read=lambda p="", q=inputs: q.pop(0), write=out.append, procs=[])
        sh.do("/new")
        assert [n for _, n in saved] == expect
        text = "\n".join(out)
        assert "crisis handed over" in text and "refused (decision_whether_a_student_is_suspended)" in text
        assert ("not saved" in text) is (answer == "n")
