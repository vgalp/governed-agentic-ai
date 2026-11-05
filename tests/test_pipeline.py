"""The request pipeline end to end (agents/pipeline), with fakes for everything outside it: Kafka,
the model, the gateway, masking, OPA and the classifiers. These are the tests the planner never
had while the pipeline lived inside its Kafka loop."""

import json

from agents.pipeline import Runtime, checks, handle, stages
from guardrails.rules import GuardrailDecision
from profiles.loader import load_profile
from router.router import RouteDecision


class FakeProducer:
    def __init__(self):
        self.messages = []

    def produce(self, topic, key=None, value=None):
        self.messages.append((topic, key, json.loads(value)))

    def flush(self):
        pass

    def poll(self, timeout=0):
        pass

    def topic(self, name):
        return [m for t, _, m in self.messages if t == name]


class FakeAudit:
    def __init__(self, producer):
        self.events, self.producer = [], producer

    def emit(self, topic, event, flush=False):
        self.events.append((topic, event))
        return event


def fake_mask(text, profile):
    out, mapping = text, {}
    for i, name in enumerate(n for n in ("Harold Brinkley", "Maria Lopez") if n in text):
        tok = f"[PERSON_{i + 1}]"
        out, mapping[tok] = out.replace(name, tok), name
    return out, mapping


def make_runtime(profile_name, tool_results=None, answer="A fine answer.", judge=None, monkeypatch=None):
    P = load_profile(profile_name)
    producer = FakeProducer()
    calls = {"chat": [], "tools": []}

    def chat(model, messages, purpose="answer"):
        calls["chat"].append((model["name"], purpose))
        if purpose == "judge":
            return judge or '{"unsupported": [], "verdict": "supported"}'
        return answer

    def call_tool(agent, tool, args, request_id, profile, audit_args=None, role=None, on_progress=None, **kw):
        calls["tools"].append((tool, args, audit_args, role))
        return (tool_results or {}).get(tool, {"results": []})

    from guardrails import faithfulness

    rt = Runtime(
        P,
        producer,
        FakeAudit(producer),
        chat,
        call_tool,
        fake_mask,
        lambda profile, path, inp: {"allowed": True},
        "planner",
        {"question": "guard", "answer": "llama-guard3:8b", "judge": None, "input_agent": None},
        faithfulness.load_config(P),
    )
    rt.calls = calls
    # the classifiers are models: safe unless a test says otherwise; toxicity off; routing local, no OPA
    monkeypatch.setattr(stages, "input_check", lambda text, profile=None: GuardrailDecision(True))
    monkeypatch.setattr(stages, "crisis_over_rule", lambda *a, **k: None)
    monkeypatch.setattr(checks, "classifier_check", lambda q, a, profile=None: GuardrailDecision(True))
    monkeypatch.setattr(checks.toxicity, "score", lambda text: None)
    local = P.models[P.routing["local_model"]]
    monkeypatch.setattr(
        stages,
        "route",
        lambda P, mapping, used, rid, agent, role: RouteDecision(
            "local", P.routing["local_model"], local, P.routing["local_input"] == "masked", ["test"]
        ),
    )
    monkeypatch.setattr(stages, "audit_route", lambda *a, **k: None)
    monkeypatch.setattr(stages, "fallbacks", lambda P, d: [])
    return rt


def responses(rt):
    return rt.producer.topic("chat.responses")


def trace_steps(rt):
    return [(m["agent"], m["step"], m["outcome"]) for m in rt.producer.topic("agent.trace")]


# --- the everyday assistant: no roles, notes from the knowledge base ------------------------------


def test_a_plain_question_is_answered_by_the_model_with_its_notes(monkeypatch):
    rt = make_runtime(
        "general-assistant",
        {"search_knowledge": {"results": [{"id": "gen-001", "title": "Tip", "text": "Call 988."}]}},
        answer="You can call 988 [gen-001].",
        monkeypatch=monkeypatch,
    )
    out = handle(rt, {"request_id": "r1", "message": "Where can I get help at night?"})
    assert out.answer == "You can call 988 [gen-001]." and out.sources == [{"id": "gen-001", "title": "Tip"}]
    assert out.guardrail is None and out.route_info["target"] == "local"
    [resp] = responses(rt)
    assert resp["answer"] == out.answer and resp["metrics"]["outcome"] == "answered"
    assert rt.calls["chat"] == [("qwen2.5:7b", "answer")]
    steps = trace_steps(rt)
    assert steps[0] == ("intake", "received", "info") and ("answer", "reply", "info") == steps[-1]
    assert ("answer", "grounding", "pass") in steps and ("answer", "output_classifier", "pass") in steps
    # the model saw the masked text and the notes; the audit recorded masked text
    inputs = [e for t, e in rt.audit.events if t == "audit.model_inputs"]
    assert inputs[0]["notes_used"] == ["gen-001"] and inputs[0]["sent_masked"]


def test_a_crisis_is_stopped_before_masking_tools_or_any_model(monkeypatch):
    rt = make_runtime("general-assistant", monkeypatch=monkeypatch)
    out = handle(rt, {"request_id": "r2", "message": "I want to kill myself tonight"})
    assert out.guardrail == {"stage": "input", "layer": "rules", "category": "crisis"}
    assert "988" in out.answer and rt.calls["chat"] == [] and rt.calls["tools"] == []
    assert responses(rt)[0]["metrics"]["outcome"] == "declined"
    assert [e for t, e in rt.audit.events if t == "audit.guardrails"][0]["category"] == "crisis"


def test_personal_details_are_masked_for_the_model_and_restored_in_the_answer(monkeypatch):
    rt = make_runtime(
        "general-assistant", answer="I can't call [PERSON_1], but here is how to reach them.", monkeypatch=monkeypatch
    )
    out = handle(rt, {"request_id": "r3", "message": "Can you call Maria Lopez for me?"})
    assert "Maria Lopez" in out.answer and "[PERSON_1]" not in out.answer
    assert rt.calls["tools"][0][1] == {"query": "Can you call [PERSON_1] for me?"}  # the tool got masked text


def test_an_invented_citation_is_blocked_by_the_grounding_check(monkeypatch):
    rt = make_runtime("general-assistant", answer="The president is X, fetched from [W1234567890].", monkeypatch=monkeypatch)
    out = handle(rt, {"request_id": "r4", "message": "Tell me something."})
    assert out.guardrail == {"stage": "output", "layer": "grounding", "category": "ungrounded"}
    assert responses(rt)[0]["metrics"]["outcome"] == "withheld"
    assert ("answer", "grounding", "block") in trace_steps(rt) and ("answer", "output_classifier", "pass") not in trace_steps(rt)


def test_a_research_question_with_nothing_read_gets_the_fixed_reply_and_no_model(monkeypatch):
    rt = make_runtime(
        "general-assistant",
        {
            "web_research": {
                "results": [],
                "note": "the search returned no results",
                "query_sent": "x",
                "hits": 0,
                "searched": "duckduckgo",
            }
        },
        monkeypatch=monkeypatch,
    )
    out = handle(rt, {"request_id": "r5", "message": "Search the web for who the president of the USA is"})
    assert out.guardrail["category"] == "no_results" and "the search returned no results" in out.answer
    assert rt.calls["chat"] == []


def test_the_judge_runs_for_web_answers_and_warn_mode_adds_a_note(monkeypatch):
    page = {"id": "web-1a2b3c4d", "title": "Page", "text": "Mark Carney is prime minister.", "url": "https://x.gov/a"}
    rt = make_runtime(
        "general-assistant",
        {"web_research": {"results": [page], "query_sent": "pm", "hits": 3, "searched": "duckduckgo"}},
        answer="The PM is Mark Carney [web-1a2b3c4d], since March.",
        judge='{"unsupported": [{"claim": "since March", "why": "not in sources"}], "verdict": "unsupported"}',
        monkeypatch=monkeypatch,
    )
    out = handle(rt, {"request_id": "r6", "message": "Search the web for the prime minister of Canada"})
    assert out.guardrail is None and "could not confirm every detail" in out.answer  # mode: warn
    assert out.sources == [{"id": "web-1a2b3c4d", "title": "Page", "url": "https://x.gov/a"}]
    assert ("qwen2.5:7b", "answer") in rt.calls["chat"] and ("llama3.1:8b", "judge") in rt.calls["chat"]
    # a contradicted fact withholds, and the pages are still listed
    rt = make_runtime(
        "general-assistant",
        {"web_research": {"results": [page], "query_sent": "pm", "hits": 3, "searched": "duckduckgo"}},
        answer="The PM is Justin Trudeau [web-1a2b3c4d].",
        judge='{"unsupported": [{"claim": "Justin Trudeau", "why": "contradicted: Mark Carney"}], "verdict": "unsupported"}',
        monkeypatch=monkeypatch,
    )
    out = handle(rt, {"request_id": "r7", "message": "Search the web for the prime minister of Canada"})
    assert out.guardrail["category"] == "unfaithful" and out.sources[0]["id"] == "web-1a2b3c4d"


def test_no_model_available_gives_the_fixed_reply(monkeypatch):
    rt = make_runtime("general-assistant", monkeypatch=monkeypatch)
    from llm.providers import ModelUnavailable

    def down(*a, **k):
        raise ModelUnavailable("ollama down")

    rt.chat = down
    out = handle(rt, {"request_id": "r8", "message": "Hello"})
    assert out.guardrail["category"] == "model_unavailable" and responses(rt)[0]["metrics"]["outcome"] == "failed"


# --- the clinic: roles, records, direct tools, changes --------------------------------------------


def test_a_clinic_question_needs_a_role_and_records_go_to_the_tool_unmasked(monkeypatch):
    rt = make_runtime("clinic-assistant", monkeypatch=monkeypatch)
    out = handle(rt, {"request_id": "c1", "message": "When is Harold Brinkley's next visit?"})
    assert out.guardrail["category"] == "role_required" and rt.calls["tools"] == []
    rt = make_runtime(
        "clinic-assistant",
        {
            "search_records": {
                "results": [
                    {
                        "id": "pt-0000a001",
                        "title": "Patient: Harold Brinkley",
                        "text": "Harold Brinkley, born 1948-03-14.",
                        "data_classes": ["patient_details"],
                    }
                ]
            }
        },
        answer="Harold Brinkley was born 1948-03-14 [pt-0000a001].",
        monkeypatch=monkeypatch,
    )
    out = handle(rt, {"request_id": "c2", "message": "When was Harold Brinkley born?", "role": "front_desk"})
    assert out.guardrail is None and out.sources[0]["id"] == "pt-0000a001"
    tool, args, audit_args, role = next(c for c in rt.calls["tools"] if c[0] == "search_records")
    assert args == {"query": "When was Harold Brinkley born?"} and audit_args == {"query": "When was [PERSON_1] born?"}
    assert role == "front_desk" and responses(rt)[0]["access"]["role"] == "front_desk"


def test_a_direct_tool_answers_in_its_own_words_and_no_model_runs(monkeypatch):
    rt = make_runtime(
        "clinic-assistant",
        {
            "chart_brief": {
                "results": [{"id": "cond-1", "title": "Problem list", "text": "AF"}],
                "answer": "Chart brief for Harold Brinkley [cond-1].",
            }
        },
        monkeypatch=monkeypatch,
    )
    out = handle(rt, {"request_id": "c3", "message": "Give me a brief on Harold Brinkley", "role": "clinician"})
    assert out.answer == "Chart brief for Harold Brinkley [cond-1]." and out.route_info["target"] == "none"
    assert rt.calls["chat"] == [] and [c[0] for c in rt.calls["tools"]] == ["chart_brief"]
    assert responses(rt)[0]["metrics"]["outcome"] == "answered"


def test_a_direct_tool_the_role_may_not_use_is_refused_by_name(monkeypatch):
    from gateway.gateway import ToolCallDenied

    rt = make_runtime("clinic-assistant", monkeypatch=monkeypatch)

    def denied(agent, tool, *a, **k):
        raise ToolCallDenied(tool)

    rt.call_tool = denied
    out = handle(rt, {"request_id": "c4", "message": "Give me a brief on Harold Brinkley", "role": "front_desk"})
    assert out.guardrail["category"] == "tool_denied" and "chart brief" in out.answer.lower() and rt.calls["chat"] == []


def test_a_failing_tool_is_no_results_not_a_dead_pipeline(monkeypatch):
    from gateway.gateway import ToolFailed

    rt = make_runtime("clinic-assistant", monkeypatch=monkeypatch)

    def broken(agent, tool, *a, **k):
        raise ToolFailed(f"{tool}: boom")

    rt.call_tool = broken
    out = handle(rt, {"request_id": "c5", "message": "When was Harold Brinkley born?", "role": "front_desk"})
    assert out.guardrail is None  # the model answered, told the records did not answer
    assert ("records", "search_records", "error") in trace_steps(rt)


def test_a_change_request_becomes_a_proposal_a_person_must_approve(monkeypatch):
    rt = make_runtime(
        "clinic-assistant",
        {
            "search_records": {
                "results": [
                    {
                        "id": "apt-0001",
                        "title": "Upcoming appointment",
                        "text": "Harold Brinkley: Follow-up visit on 2026-11-03 at 10:00 (scheduled).",
                        "data_classes": ["appointments"],
                    }
                ]
            }
        },
        answer='{"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"}',
        monkeypatch=monkeypatch,
    )
    out = handle(
        rt,
        {"request_id": "c6", "message": "Move Harold Brinkley's next appointment to November 13 at 10:00.", "role": "front_desk"},
    )
    assert out.approval and out.approval["status"] == "pending" and "approval" in out.answer.lower()
    assert rt.calls["chat"] == [("mistral:7b", "proposal")] or rt.calls["chat"][0][1] == "proposal"
    assert rt.producer.topic("approvals.requested")[0]["action"] == "reschedule_appointment"
    assert responses(rt)[0]["metrics"]["outcome"] == "proposed"


def test_a_request_that_raises_gets_the_unavailable_reply_and_an_audit_entry(monkeypatch):
    rt = make_runtime("general-assistant", monkeypatch=monkeypatch)
    stages.fail(rt, {"request_id": "r9", "message": "x"}, RuntimeError("boom"))
    [resp] = responses(rt)
    assert resp["guardrail"]["category"] == "pipeline_error" and resp["metrics"]["outcome"] == "failed"
    assert "try again" in resp["answer"].lower()
    assert [e for t, e in rt.audit.events if t == "audit.guardrails"][0]["reason"] == "RuntimeError"


def test_the_stages_and_checks_are_lists_a_reviewer_can_read():
    assert [s.__name__ for s in stages.STAGES] == [
        "intake",
        "role_check",
        "masking",
        "detect_change",
        "call_tools",
        "settle_tools",
        "propose",
        "answer",
        "check_answer",
    ]
    assert [c.layer for c in checks.OUTPUT_CHECKS] == ["rules", "grounding", "classifier", "faithfulness"]



def test_the_web_switch_from_the_chat_page_reaches_the_tool_choice(monkeypatch):
    page = {"id": "web-1a2b3c4d", "title": "Page", "text": "Flu shots are advised every year.", "url": "https://x.gov/a"}
    results = {"web_research": {"results": [page], "query_sent": "flu", "hits": 1, "searched": "duckduckgo"},
               "search_knowledge": {"results": []}}
    rt = make_runtime("general-assistant", results, answer="Every year [web-1a2b3c4d].",
                      judge='{"unsupported": [], "verdict": "supported"}', monkeypatch=monkeypatch)
    out = handle(rt, {"request_id": "w1", "message": "How often should adults get a flu shot?", "web": True})
    assert [c[0] for c in rt.calls["tools"]] == ["web_research"]    # no web words, but the switch was on
    assert out.sources and out.sources[0]["id"] == "web-1a2b3c4d"
    rt = make_runtime("general-assistant", results, answer="Once a year, in general.", monkeypatch=monkeypatch)
    out = handle(rt, {"request_id": "w2", "message": "Search the web: how often should adults get a flu shot?",
                      "web": False})
    assert "web_research" not in [c[0] for c in rt.calls["tools"]]   # switched off: no page read, whatever the wording
