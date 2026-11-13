"""gaai, the command line and operator console (ADR 033): what it reads, the commands, and the local
assistant's rules (reads freely, changes only with the operator's yes, no patient data, no approvals).

No network: a fake API and a fake Ollama answer.
"""

import json

import pytest

from cli import agent as ag
from cli import platform as pf
from cli import ui
from cli.shell import Shell, status_lines


@pytest.fixture(autouse=True)
def plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    """The platform's API, running the clinic-oracle profile."""

    def __init__(self, up=True):
        self.up, self.posted = up, []

    def get(self, url, params=None, timeout=None):
        if not self.up:
            raise pf.requests.ConnectionError("refused")
        path = url.removeprefix("http://api.test")
        body = {"/profile": {"name": "clinic-oracle", "title": "Clinic staff assistant (Oracle Health sandbox)"},
                "/health": {"status": "ok", "problems": [], "warnings": []},
                "/audit/verify": {"scope": "clinic-oracle", "events": 12, "chains": 2, "intact": True, "problems": []},
                "/approvals": [{"id": "ap-123456789", "status": "pending", "action": "update_appointment",
                                "created_at": "2026-11-12T10:00:00"}],
                "/chat/r-1": {"status": "done", "request_id": "r-1", "answer": "We need 24 hours' notice.",
                              "route": {"model": "mistral"}}}.get(path, {"status": "pending"})
        return Resp(body)

    def post(self, url, json=None, timeout=None):
        self.posted.append(json)
        return Resp({"request_id": "r-1"})


def FakeApi(up=True):  # noqa: N802
    return pf.Api("http://api.test", session=FakeHttp(up))


PS = ["/x/.venv/bin/python -m ops.supervise records -- -m mcp_servers.records.server",
      "/x/.venv/bin/python -m mcp_servers.records.server",
      "/x/.venv/bin/python -m uvicorn api.main:app --port 8000",
      "/x/.venv/bin/python -m agents.planner", "/x/.venv/bin/python -m agents.planner",
      "grep agents.planner"]


# --- what gaai reads ------------------------------------------------------------------------------------

def test_the_services_come_from_dev_sh():
    names = {s.name: s.port for s in pf.services()}
    assert names["chart-brief"] == 8106 and names["api"] == 8000 and names["planner"] is None
    assert len(names) == 11


def test_service_states_count_the_worker_not_its_supervisor_and_find_stale_copies():
    st = {s["name"]: s["state"] for s in pf.service_states(PS)}
    assert st["records"] == "running"               # the supervisor's own line is not a second copy
    assert st["api"] == "running" and st["planner"] == "duplicated" and st["executor"] == "stopped"


def test_profiles_say_where_their_records_come_from():
    p = {x["name"]: x["records"] for x in pf.profiles()}
    assert p["clinic-oracle"] == "FHIR fhir-open.cerner.com (none)"
    assert p["clinic-meditech"].endswith("(smart_user)") and p["clinic-assistant"] == "database"


def test_a_log_name_cannot_reach_outside_the_logs_folder():
    with pytest.raises(ValueError):
        pf.log_tail("../.env")


def test_a_question_goes_through_the_pipeline_and_waits_for_the_answer():
    api = FakeApi()
    r = api.ask("What is our cancellation policy?", "clinician", sleep=lambda s: None)
    assert r["answer"] == "We need 24 hours' notice."
    assert api.http.posted == [{"message": "What is our cancellation policy?", "role": "clinician", "user_id": "gaai-cli"}]


def test_an_api_that_is_down_is_an_answer_not_a_crash():
    snap = pf.snapshot(FakeApi(up=False), PS)
    assert snap["profile"] is None and snap["health"] is None and snap["running"] == 2


def test_the_start_screen_shows_the_state_in_plain_words():
    snap = pf.snapshot(FakeApi(), PS)
    text = ui.ANSI.sub("", "\n".join(status_lines(snap, (True, "qwen3:8b"))))
    assert "profile   clinic-oracle" in text and "services  2 of 11 running" in text
    assert "audit     chain intact: 12 events in 2 chain(s)" in text and "approvals 1 waiting" in text
    assert "assistant qwen3:8b, on this computer" in text


def test_panels_line_up():
    lines = ui.panel("status", ["short", "x" * 300]).splitlines()
    assert len({ui.visible_len(x) for x in lines}) == 1


# --- the local assistant ---------------------------------------------------------------------------------

class FakeOllama:
    """Answers /api/tags and /api/chat from a script of assistant messages."""

    def __init__(self, script, models=("qwen3:8b",)):
        self.script, self.models, self.sent = list(script), models, []

    def get(self, url, timeout=None):
        return Resp({"models": [{"name": m} for m in self.models]})

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        return Resp({"message": self.script.pop(0)})


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def make_agent(script, confirm=lambda t, a: False, api=None):
    seen = []
    a = ag.Agent(ag.tools(api or FakeApi()), confirm=confirm, show=lambda e, d: seen.append(d["name"]),
                 session=FakeOllama(script))
    return a, seen


def test_the_assistant_reads_with_tools_then_answers():
    a, seen = make_agent([call("audit_verify", scope="profile"),
                          {"role": "assistant", "content": "<think>x</think>The audit chain is intact (12 events)."}])
    assert a.ask("is the audit log ok?") == "The audit chain is intact (12 events)."
    assert seen == ["audit_verify"]
    tool_msg = next(m for m in a.messages if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["intact"] is True
    sent = a.http.sent[0]
    assert sent["model"] == "qwen3:8b" and sent["stream"] is False
    assert {t["function"]["name"] for t in sent["tools"]} >= {"status", "logs", "restart"}


def test_a_change_waits_for_the_operators_yes(monkeypatch):
    ran = []
    monkeypatch.setattr(pf, "dev", lambda action, profile=None, **kw: ran.append((action, profile)) or {"ok": True})
    asked = []
    a, _ = make_agent([call("restart", profile="clinic-oracle"), {"role": "assistant", "content": "Not restarted."}],
                      confirm=lambda tool, args: asked.append((tool.name, args)) or False)
    assert a.ask("switch to oracle") == "Not restarted."
    assert asked == [("restart", {"profile": "clinic-oracle"})] and ran == []
    assert json.loads([m for m in a.messages if m["role"] == "tool"][0]["content"])["declined"] is True

    b, _ = make_agent([call("restart", profile="clinic-oracle"), {"role": "assistant", "content": "Restarted."}],
                      confirm=lambda tool, args: True)
    b.ask("switch to oracle")
    assert ran == [("restart", "clinic-oracle")]


def test_the_assistant_has_no_tool_for_patient_records_questions_or_approving():
    names = set(ag.tools(FakeApi()))
    assert names == {"status", "health", "audit_verify", "profiles", "logs", "approvals", "fhir_check",
                     "start", "restart", "stop"}
    assert {n for n, t in ag.tools(FakeApi()).items() if t.changes} == {"start", "restart", "stop"}


def test_the_record_system_check_reads_no_patient(monkeypatch):
    got = []
    monkeypatch.setattr(pf, "fhir_check", lambda profile, patient=None, **kw: got.append(patient) or {"ok": True})
    a, _ = make_agent([call("fhir_check", profile="clinic-oracle", patient="Wilm Smart 1990-09-15"),
                       {"role": "assistant", "content": "Connected."}])
    a.ask("check oracle")
    assert got == [None]                       # an argument the tool does not take is dropped


def test_an_unknown_tool_or_a_failing_one_is_told_to_the_model():
    a, _ = make_agent([call("delete_everything"), call("logs", service="../etc"),
                       {"role": "assistant", "content": "done"}])
    a.ask("x")
    results = [json.loads(m["content"]) for m in a.messages if m["role"] == "tool"]
    assert results[0] == {"error": "no tool named delete_everything"} and "ValueError" in results[1]["error"]


def test_the_model_must_be_in_ollama():
    a = ag.Agent({}, confirm=lambda t, x: False, session=FakeOllama([], models=("mistral:latest",)))
    ok, what = a.available()
    assert not ok and "ollama pull qwen3:8b" in what


# --- the console ---------------------------------------------------------------------------------------

def console(lines=(), api=None, script=()):
    out, inputs = [], list(lines)
    api = api or FakeApi()
    agent = ag.Agent(ag.tools(api), confirm=lambda t, a: False, session=FakeOllama(script))
    sh = Shell(api=api, agent=agent, read=lambda prompt="": inputs.pop(0), write=out.append, procs=PS)
    agent.confirm = sh.confirm
    return sh, out


def test_ask_sends_a_question_through_the_pipeline():
    sh, out = console()
    sh.do("/ask clinician What is our cancellation policy?")
    assert any("We need 24 hours' notice." in o for o in out) and any("model mistral" in o for o in out)


def test_switching_profile_asks_first_and_does_nothing_on_no(monkeypatch):
    ran = []
    monkeypatch.setattr(pf, "dev", lambda *a, **k: ran.append(a) or {"ok": True})
    sh, out = console(lines=["n"])
    sh.do("/use clinic-oracle")
    assert ran == [] and "not done (you answered no)" in out[-1]
    sh, out = console(lines=["y"])
    sh.do("/use clinic-oracle")
    assert ran == [("restart", "clinic-oracle")]


def test_commands_explain_their_mistakes():
    sh, out = console()
    sh.do("/use nowhere")
    sh.do("/logs")
    sh.do("/frobnicate")
    assert "no profile named nowhere" in out[0] and "which service?" in out[1] and "no command /frobnicate" in out[2]


def test_plain_words_go_to_the_assistant():
    sh, out = console(script=[{"role": "assistant", "content": "Everything is running."}])
    sh.do("is everything running?")
    assert out[-1].strip() == "Everything is running."


def test_quit_leaves():
    sh, _ = console()
    assert sh.do("/quit") is False and sh.do("") is True


def test_status_exit_code_says_whether_everything_runs(monkeypatch, capsys):
    from cli import main as m
    monkeypatch.setattr(pf, "snapshot", lambda api=None, procs=None, root=pf.ROOT: {
        "services": [], "running": 3, "total": 11, "profile": None, "next_profile": "x", "pending": 0})
    assert m.main(["status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["running"] == 3


def test_switch_to_epic_names_the_profile_asks_and_shows_the_restart_as_it_runs(monkeypatch):
    """"switch to epic": the tool takes only real profile names, the operator is asked, and the
    restart's output is shown line by line, with the spinner held while the question waits."""
    from cli import ui as u
    ran, held = [], []

    def fake_dev(action, profile=None, on_line=None, **kw):
        ran.append((action, profile))
        for line in ("Stopping services", "✓ api", "Ready."):
            on_line(line)
        return {"ok": True, "output": "Ready."}
    monkeypatch.setattr(pf, "dev", fake_dev)
    out, answers = [], ["y"]
    api = FakeApi()

    def read(prompt=""):
        held.append(u._quiet.is_set())
        out.append(prompt)
        return answers.pop(0)
    sh = Shell(api=api, read=read, write=out.append, procs=PS)
    sh.agent.http = FakeOllama([call("restart", profile="clinic-epic"),
                                {"role": "assistant", "content": "Restarted with clinic-epic."}])
    enum = sh.agent.tools["restart"].params["profile"]["enum"]
    assert "clinic-epic" in enum and "epic" not in enum
    sh.do("switch to epic")
    assert ran == [("restart", "clinic-epic")] and held == [True]
    assert any("Restart the platform with profile clinic-epic. Go ahead?" in o for o in out)
    assert [o for o in out if o.startswith("  │ ")] == ["  │ Stopping services", "  │ ✓ api", "  │ Ready."]
    assert out[-1].strip() == "Restarted with clinic-epic."


def test_after_a_no_the_same_change_can_be_asked_for_again(monkeypatch):
    """The model is told a no was for one proposal; the operator sees that nothing was done."""
    ran = []
    monkeypatch.setattr(pf, "dev", lambda action, profile=None, **kw: ran.append(profile) or {"ok": True})
    out, answers = [], ["", "y"]
    sh = Shell(api=FakeApi(), read=lambda p="": answers.pop(0), write=out.append, procs=PS)
    sh.agent.http = FakeOllama([call("start", profile="clinic-epic"), {"role": "assistant", "content": "Not started."},
                                call("start", profile="clinic-epic"), {"role": "assistant", "content": "Started."}])
    sh.do("start the epic profile")
    assert ran == [] and any("not done (you answered no)" in o for o in out)
    note = json.loads([m for m in sh.agent.messages if m["role"] == "tool"][0]["content"])["note"]
    assert "propose it again" in note and "Do not retry" not in note
    sh.do("start the epic profile")
    assert ran == ["clinic-epic"]
    assert "not the pipeline's approvals" in ag.SYSTEM
