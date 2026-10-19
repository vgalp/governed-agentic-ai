"""The governed MCP endpoint for outside assistants (ADR 015): who is calling comes from the
organization's own token check, tenant data is fenced per caller, the input checks run on tool
arguments, and results for outside AI services are masked. No OPA, Kafka or model: fakes."""

import copy
import json
import shutil
from dataclasses import replace

import pytest
import yaml

from gateway import endpoint as ep
from gateway import gateway
from guardrails.rules import GuardrailDecision
from profiles.loader import ProfileError, load_profile, load_profile_from

OUTSIDE = {"agent": "outside",
           "listen": {"host": "127.0.0.1", "port": 8110},
           "auth": {"kind": "introspection", "url": "http://127.0.0.1:8300/introspect",
                    "secret_env": "PLATFORM_INTROSPECT_SECRET", "cache_seconds": 30},
           "mask_results": "external"}


def _edit(path, change):
    d = yaml.safe_load(path.read_text())
    change(d)
    path.write_text(yaml.safe_dump(d, sort_keys=False))


@pytest.fixture
def platform(tmp_path):
    """The county profile turned into a small multi-tenant platform: two tenant-scoped finance
    tools and a policy tool, served to outside assistants only."""
    dst = tmp_path / "county-benefits"
    shutil.copytree(load_profile("county-benefits").path, dst,
                    ignore=shutil.ignore_patterns("*.db", "sample_csv"))

    def tools(d):
        d["finance_summary"] = {"description": "Monthly finances for your agency", "kind": "read",
                                "server": "http://127.0.0.1:8201/mcp", "data_label": "agency_finance",
                                "lane": "records", "tenant_scoped": True}
        d["visit_costs"] = {"description": "Visit costs per patient", "kind": "read",
                            "server": "http://127.0.0.1:8201/mcp", "data_label": "case_record",
                            "lane": "records", "tenant_scoped": True}
    _edit(dst / "tools.yaml", tools)

    def profile(d):
        d["agents"]["outside"] = {"tools": ["finance_summary", "visit_costs", "search_knowledge"]}
        d["outside"] = "outside.yaml"
    _edit(dst / "profile.yaml", profile)

    def roles(d):
        d["roles"]["finance_analyst"] = {"title": "Finance analyst", "data_classes": [],
                                         "tools": ["finance_summary", "visit_costs", "search_knowledge"]}
        d["roles"]["billing"] = {"title": "Billing", "data_classes": [], "tools": ["search_knowledge"]}
    _edit(dst / "roles.yaml", roles)
    (dst / "outside.yaml").write_text(yaml.safe_dump(OUTSIDE, sort_keys=False))
    return dst


def outside_file(folder, change):
    d = copy.deepcopy(OUTSIDE)
    change(d)
    (folder / "outside.yaml").write_text(yaml.safe_dump(d, sort_keys=False))


# --- the profile: outside.yaml and tenant-scoped tools -------------------------------------------

def test_platform_profile_loads_with_outside_settings(platform):
    p = load_profile_from(platform)
    assert p.outside.agent == "outside" and p.outside.port == 8110
    assert p.outside.introspect_url.endswith("/introspect") and p.outside.mask_results == "external"
    assert p.tools["finance_summary"].tenant_scoped and not p.tools["search_knowledge"].tenant_scoped
    assert p.policy_data()["tools"]["finance_summary"] == {"kind": "read", "tenant_scoped": True}


def test_profiles_in_the_repo_have_no_outside_endpoint():
    for name in ("adhd-assistant", "clinic-assistant", "county-benefits"):
        assert load_profile(name).outside is None


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(agent="planner"), "own agent grant"),
    (lambda d: d.update(agent="nobody"), "not in profile.yaml agents"),
    (lambda d: d["auth"].update(kind="password"), "auth.kind"),
    (lambda d: d["auth"].update(secret_env="hunter2-secret"), "NAME of an environment variable"),
    (lambda d: d["auth"].update(url="ftp://x"), "auth.url"),
    (lambda d: d.update(mask_results="sometimes"), "mask_results"),
    (lambda d: d["listen"].update(port=70000), "listen.port"),
])
def test_outside_settings_are_checked(platform, change, message):
    outside_file(platform, change)
    with pytest.raises(ProfileError, match=message):
        load_profile_from(platform)


def test_outside_agent_may_never_change_anything(platform):
    _edit(platform / "profile.yaml", lambda d: d["agents"]["outside"]["tools"].append("release_payment"))
    with pytest.raises(ProfileError):
        load_profile_from(platform)


def test_tenant_data_only_through_the_outside_endpoint(platform):
    _edit(platform / "profile.yaml", lambda d: d["agents"]["planner"]["tools"].append("finance_summary"))
    with pytest.raises(ProfileError, match="only served through the outside endpoint"):
        load_profile_from(platform)
    _edit(platform / "profile.yaml", lambda d: (d["agents"]["planner"]["tools"].remove("finance_summary"),
                                                d.pop("outside")))
    with pytest.raises(ProfileError, match="needs outside.yaml"):
        load_profile_from(platform)


def test_tenant_scoped_is_for_read_tools_only(platform):
    _edit(platform / "tools.yaml", lambda d: d["release_payment"].update(tenant_scoped=True))
    with pytest.raises(ProfileError, match="for read tools"):
        load_profile_from(platform)


def test_profile_dir_loads_a_profile_from_outside_the_repo(platform, monkeypatch):
    monkeypatch.setenv("PROFILE_DIR", str(platform))
    monkeypatch.delenv("PROFILE", raising=False)
    load_profile.cache_clear()
    try:
        p = load_profile()
        assert p.path == platform.resolve() and p.outside is not None
        assert load_profile("adhd-assistant").name == "adhd-assistant"     # others still by name
        monkeypatch.setenv("PROFILE", "adhd-assistant")
        load_profile.cache_clear()
        with pytest.raises(ProfileError, match="PROFILE_DIR"):
            load_profile()
    finally:
        load_profile.cache_clear()


# --- the gateway: the caller's tenant, always ---------------------------------------------------

def gateway_fakes(monkeypatch, rows):
    events, seen = [], {}
    monkeypatch.setattr(gateway, "_is_allowed", lambda p, a, t, r, tenant=None: True)
    monkeypatch.setattr(gateway, "_audit", events.append)

    async def fake_call(url, tool, args):
        seen.update(args)
        return {"results": rows}
    monkeypatch.setattr(gateway, "_call", fake_call)
    return events, seen


def test_gateway_sets_the_tenant_and_drops_other_tenants(platform, monkeypatch):
    p = load_profile_from(platform)
    rows = [{"tenant_id": "agency-a", "month": "2026-09"}, {"tenant_id": "agency-b", "month": "2026-09"},
            {"month": "2026-08"}]
    events, seen = gateway_fakes(monkeypatch, rows)
    out = gateway.call_tool("outside", "finance_summary", {"period": "Q3", "tenant_id": "agency-b"}, "r1", p,
                            role="finance_analyst", tenant="agency-a", client="kiro-a")
    assert seen["tenant_id"] == "agency-a"                        # the caller's, not the one it sent
    assert out["results"] == [rows[0]]
    e = events[-1]
    assert e["tenant"] == "agency-a" and e["client"] == "kiro-a" and e["dropped_other_tenant"] == 2
    assert "tenant_id" not in e["args"]


def test_gateway_refuses_tenant_data_without_a_tenant(platform, monkeypatch):
    p = load_profile_from(platform)
    events, _ = gateway_fakes(monkeypatch, [])
    with pytest.raises(gateway.ToolCallDenied):
        gateway.call_tool("outside", "finance_summary", {}, "r1", p, role="finance_analyst", client="kiro")
    assert events[-1]["decision"] == "deny" and events[-1]["tenant"] is None


# --- the endpoint: who, which tools, checks, what leaves ----------------------------------------

def who(role="finance_analyst", tenant="agency-a", location="external", allowed=True):
    """An assistant whose organization allows outside AI (unless allowed=False)."""
    return ep.Identity("kiro-a", "Kiro, analyst", tenant, role, location, allowed)


def by_word(text, profile):
    """A fake check: crisis for 'hurt', decision for 'code higher', else allow."""
    t = text.lower()
    if "hurt" in t:
        return "crisis", GuardrailDecision(False, "crisis", "fake"), "rules"
    if "code higher" in t:
        return "decision", GuardrailDecision(False, "eligibility_decision", "fake"), "rules"
    return "allow", GuardrailDecision(True), "rules"


@pytest.fixture
def endpoint(platform):
    calls, audits = [], []

    def call(agent, tool, args, request_id, profile, audit_args=None, role=None, tenant=None, client=None):
        calls.append({"agent": agent, "tool": tool, "args": args, "audit_args": audit_args,
                      "role": role, "tenant": tenant, "client": client})
        return {"results": [{"tenant_id": tenant, "patient": "Mary Jones", "cost": 120.5}]}

    e = ep.Endpoint(load_profile_from(platform), check=by_word, call=call,
                    mask=lambda t: t.replace("Mary Jones", "[PERSON_1]"),
                    audit=lambda topic, ev: audits.append((topic, ev)))
    return e, calls, audits


def test_allowed_tools_follow_the_role(platform):
    p = load_profile_from(platform)
    assert ep.allowed_tools(p, "finance_analyst") == ["finance_summary", "visit_costs", "search_knowledge"]
    assert ep.allowed_tools(p, "billing") == ["search_knowledge"]
    assert ep.allowed_tools(p, "nobody") == [] and ep.allowed_tools(p, None) == []


def test_a_call_goes_through_the_gateway_as_the_caller(endpoint):
    e, calls, audits = endpoint
    out = e.handle(who(), "finance_summary", {"period": "last quarter", "tenant_id": "agency-b"})
    c = calls[-1]
    assert c["agent"] == "outside" and c["tenant"] == "agency-a" and c["client"] == "kiro-a"
    assert "tenant_id" not in c["args"]                            # the caller can't pick a tenant
    assert out["results"][0]["patient"] == "[PERSON_1]"            # external client: masked
    assert audits[-1][0] == "audit.guardrails" and audits[-1][1]["stage"] == "tool_input"


def test_local_clients_get_results_unmasked(endpoint):
    e, _, _ = endpoint
    out = e.handle(who(location="local"), "finance_summary", {"period": "Q3"})
    assert out["results"][0]["patient"] == "Mary Jones"


def test_a_tool_outside_the_role_is_refused_before_anything_runs(endpoint):
    e, calls, audits = endpoint
    out = e.handle(who(role="billing"), "finance_summary", {"period": "Q3"})
    assert out["error"] == "not_allowed" and calls == []
    assert audits[-1][1]["decision"] == "deny" and audits[-1][1]["client"] == "kiro-a"


def test_crisis_in_the_arguments_is_handed_over_not_queried(endpoint):
    e, calls, audits = endpoint
    out = e.handle(who(), "search_knowledge", {"query": "patient's son hurt her, what's the policy"})
    assert out["stopped"] == "crisis" and out["hand_over"] is True and out["reply"]
    assert calls == [] and audits[-1][1]["allowed"] is False


def test_a_decision_that_stays_with_people_is_declined(endpoint):
    e, calls, _ = endpoint
    out = e.handle(who(), "visit_costs", {"query": "which visits can we code higher"})
    assert out["stopped"] == "decision" and out["category"] == "eligibility_decision" and calls == []


def test_data_that_never_leaves_is_refused_to_outside_ai_services(endpoint):
    e, calls, audits = endpoint
    out = e.handle(who(), "visit_costs", {"period": "Q3"})          # case_record: never external
    assert out["error"] == "not_allowed" and calls == []
    assert audits[-1][1]["reason"] == "data never leaves the organization"
    e.handle(who(location="local"), "visit_costs", {"period": "Q3"})
    assert calls[-1]["tool"] == "visit_costs"


def test_audit_holds_masked_arguments_only(endpoint):
    e, calls, _ = endpoint
    e.handle(who(location="local"), "finance_summary", {"note": "for Mary Jones"})
    assert calls[-1]["args"]["note"] == "for Mary Jones" and calls[-1]["audit_args"]["note"] == "for [PERSON_1]"


def test_a_denied_or_failing_tool_gives_a_plain_error(endpoint, monkeypatch):
    e, _, _ = endpoint

    def denied(*a, **k):
        raise gateway.ToolCallDenied("no")
    e._call = denied
    assert e.handle(who(), "finance_summary", {})["error"] == "not_allowed"

    def broken(*a, **k):
        raise ConnectionError("down")
    e._call = broken
    assert e.handle(who(), "finance_summary", {}) == {"error": "tool_failed",
                                                      "message": "The tool could not run (ConnectionError)."}


def test_mask_results_always_and_never(endpoint):
    e, _, _ = endpoint
    o = e.profile.outside
    e.profile = replace(e.profile, outside=replace(o, mask_results="always"))
    assert e.handle(who(location="local"), "finance_summary", {})["results"][0]["patient"] == "[PERSON_1]"
    e.profile = replace(e.profile, outside=replace(o, mask_results="never"))
    assert e.handle(who(), "finance_summary", {})["results"][0]["patient"] == "Mary Jones"


# --- who is calling ------------------------------------------------------------------------------

class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def introspector(answers, clock=None):
    asked = []

    def post(url, json, headers, timeout):
        asked.append((json["token"], headers["X-Introspect-Secret"]))
        a = answers[json["token"]]
        if isinstance(a, Exception):
            raise a
        return a
    t = {"now": 0.0}
    i = ep.Introspector("http://x/introspect", "s3cret", 30, post=post, clock=clock or (lambda: t["now"]))
    return i, asked, t


def test_introspection_gives_identity_from_the_organization():
    i, asked, _ = introspector({"tok": Resp(200, {"active": True, "client_id": "c1", "tenant_id": "agency-a",
                                                  "role": "finance_analyst", "location": "local"})})
    w = i.identify("tok")
    assert (w.client_id, w.tenant, w.role, w.location) == ("c1", "agency-a", "finance_analyst", "local")
    assert asked == [("tok", "s3cret")]


@pytest.mark.parametrize("answer", [Resp(200, {"active": False, "client_id": "c1"}), Resp(200, {"active": True}),
                                    Resp(401, {}), Resp(200, "nonsense"), ConnectionError("down")])
def test_introspection_fails_closed(answer):
    i, _, _ = introspector({"tok": answer})
    assert i.identify("tok") is None
    assert i.identify(None) is None and i.identify("") is None


def test_introspection_is_cached_briefly_and_rechecked():
    i, asked, t = introspector({"tok": Resp(200, {"active": True, "client_id": "c1"})})
    i.identify("tok")
    i.identify("tok")
    assert len(asked) == 1
    t["now"] = 31.0                                                # a revoked client stops within 30 s
    i.identify("tok")
    assert len(asked) == 2


def test_unknown_location_is_treated_as_external():
    i, _, _ = introspector({"tok": Resp(200, {"active": True, "client_id": "c1", "location": "moon"})})
    assert i.identify("tok").location == "external"


def test_bearer_and_hidden_parameters():
    assert ep.bearer({"authorization": "Bearer abc"}) == "abc"
    assert ep.bearer({"authorization": "Basic abc"}) is None and ep.bearer({}) is None
    schema = {"type": "object", "properties": {"period": {}, "tenant_id": {}, "allowed_classes": {}},
              "required": ["period", "tenant_id"]}
    assert ep.strip_hidden(schema) == {"type": "object", "properties": {"period": {}}, "required": ["period"]}
    assert ep.texts({"a": "x", "b": ["y", {"c": "z"}], "d": 3, "e": "  "}) == ["x", "y", "z"]


def test_results_that_cannot_be_masked_are_withheld(endpoint):
    e, _, audits = endpoint

    def broken(text):
        raise RuntimeError("analyzer down")
    e._mask = broken
    out = e.handle(who(), "finance_summary", {})
    assert out["error"] == "tool_failed" and "withheld" in out["message"]
    assert audits[-1][1]["decision"] == "withheld"


def test_text_arguments_that_cannot_be_masked_stop_the_call(endpoint):
    e, _, audits = endpoint

    def broken(text):
        raise RuntimeError("analyzer down")
    e._mask = broken
    out = e.handle(who(), "search_knowledge", {"query": "overtime approval"})
    assert out["error"] == "tool_failed" and "not run" in out["message"]
    assert audits[-1][1]["decision"] == "deny" and "masking failed" in audits[-1][1]["reason"]


# --- POST /check: questions typed into an assistant that runs its own model ----------------------

def test_a_typed_question_is_checked_and_audited(endpoint):
    e, calls, audits = endpoint
    assert e.check_message(who(location="local"), "overtime approval policy") == {"allowed": True}
    out = e.check_message(who(location="local"), "the patient's son hurt her")
    assert out["allowed"] is False and out["stopped"] == "crisis" and out["hand_over"] is True
    assert out["reply"] == e.profile.response("crisis", "finance_analyst")
    topic, ev = audits[-1]
    assert topic == "audit.guardrails" and ev["stage"] == "user_message" and ev["client"] == "kiro-a"
    assert "hurt" not in str(ev) and calls == []                  # the text itself is never audited


def test_check_route_needs_a_known_token_and_sane_text(endpoint):
    from starlette.testclient import TestClient

    class Known:
        def identify(self, token):
            return who(location="local") if token == "good" else None

    e, _, _ = endpoint
    c = TestClient(ep.build_app(e.profile, Known(), {}, e))
    assert c.post("/check", json={"text": "hi"}).status_code == 401
    assert c.post("/check", json={"text": "hi"}, headers={"Authorization": "Bearer bad"}).status_code == 401
    auth = {"Authorization": "Bearer good"}
    assert c.post("/check", json={"text": "  "}, headers=auth).status_code == 400
    assert c.post("/check", json={"text": "x" * (ep.MAX_MESSAGE + 1)}, headers=auth).status_code == 400
    assert c.post("/check", content=b"not json", headers=auth).status_code == 400
    assert c.post("/check", json={"text": "overtime policy"}, headers=auth).json() == {"allowed": True}
    assert c.post("/check", json={"text": "she was hurt"}, headers=auth).json()["stopped"] == "crisis"


def test_asking_for_another_tenant_is_recorded_not_obeyed(endpoint):
    e, calls, audits = endpoint
    e.handle(who(), "finance_summary", {"tenant_id": "agency-b"})
    assert calls[-1]["tenant"] == "agency-a" and "tenant_id" not in calls[-1]["args"]
    topic, ev = audits[-1]
    assert topic == "audit.tool_calls" and ev["decision"] == "override" and ev["tenant_requested"] == "agency-b"
    assert ev["location"] == "external" and ev["tenant"] == "agency-a"
    n = len(audits)
    e.handle(who(), "finance_summary", {"tenant_id": "agency-a"})        # its own tenant: nothing to record
    assert len(audits) == n


# --- governed chat: one question = one pipeline request -------------------------------------------

from router.router import RouteDecision  # noqa: E402

CHAT = "chat-12345678-1234-1234-1234-123456789abc"


class FakeTrace:
    def __init__(self):
        self.steps = []

    def step(self, agent, step, outcome, summary, **details):
        self.steps.append((agent, step, outcome))

    def start(self, agent, step, summary):
        self.steps.append((agent, step, "running"))


@pytest.fixture
def chat(endpoint):
    e, calls, audits = endpoint
    seen = {"published": [], "routes": [], "models": [], "traces": []}

    def route(p, mapping, used, request_id, agent, role, allow_external=True):
        seen["routes"].append({"used": used, "allow_external": allow_external, "entities": sorted(mapping)})
        if allow_external:
            return RouteDecision("external", "claude", {"name": "claude-x", "provider": "anthropic"}, True,
                                 ["no personal information found; external model allowed"])
        return RouteDecision("local", "local", e.profile.local_model, True, ["administrator: local only"])

    def model(m, messages):
        seen["models"].append((m["name"], messages))
        return "<think>hmm</think>Margins for [PERSON_1] are fine."

    def trace(profile, request_id, role):
        t = FakeTrace()
        seen["traces"].append(t)
        return t

    e._mask_map = lambda t: (t.replace("Mary Jones", "[PERSON_1]"), {"[PERSON_1]": "Mary Jones"})
    e._route, e._model, e._trace = route, model, trace
    e._output_check = lambda q, a: GuardrailDecision(True)
    e._publish = lambda topic, v: seen["published"].append((topic, v))
    return e, calls, audits, seen


def test_a_governed_chat_request_goes_through_every_stage(chat):
    e, calls, audits, seen = chat
    me = replace(who(location="local"), external_model_allowed=True)
    assert e.check_message(me, "Margins for Mary Jones?", CHAT) == {"allowed": True, "request_id": CHAT}
    e.handle(me, "finance_summary", {"period": "Q3"}, CHAT)
    out = e.answer(me, CHAT)
    assert out["answer"] == "Margins for Mary Jones are fine." and out["route"]["target"] == "external"
    name, messages = seen["models"][0]
    assert "Mary Jones" not in json.dumps(messages) and "[PERSON_1]" in messages[1]["content"]   # only masked text left
    assert seen["routes"][0] == {"used": ["finance_summary"], "allow_external": True, "entities": ["[PERSON_1]"]}
    stages = [(t, ev.get("stage")) for t, ev in audits if ev["request_id"] == CHAT]
    assert ("audit.guardrails", "input") in stages and ("audit.model_inputs", None) in stages
    assert ("audit.guardrails", "output") in stages
    assert calls[0]["tenant"] == "agency-a"
    topic, resp = seen["published"][-1]
    assert topic == "chat.responses" and resp["request_id"] == CHAT and resp["guardrail"] is None
    lanes = [s[0] for s in seen["traces"][0].steps]
    assert {"intake", "records", "router", "answer"} <= set(lanes)
    assert e.answer(me, CHAT)["error"] == "not_allowed"                       # one answer per request


def test_the_administrator_setting_reaches_routing(chat):
    e, _, _, seen = chat
    me = who(location="local", allowed=False)                                # the administrator said no
    e.check_message(me, "overtime policy", CHAT)
    assert e.answer(me, CHAT)["route"]["target"] == "local"
    assert seen["routes"][0]["allow_external"] is False


def test_chat_steps_need_a_checked_question_from_the_same_client(chat):
    e, calls, _, seen = chat
    me, other = who(location="local"), ep.Identity("kiro-b", "Other", "agency-a", "finance_analyst", "local")
    assert e.handle(me, "finance_summary", {}, CHAT)["error"] == "not_allowed"          # not checked yet
    assert e.answer(me, CHAT)["error"] == "not_allowed"
    e.check_message(me, "overtime policy", CHAT)
    assert e.handle(other, "finance_summary", {}, CHAT)["error"] == "not_allowed"       # not its request
    assert e.answer(other, CHAT)["error"] == "not_allowed"
    assert e.check_message(me, "again", CHAT)["error"] == "already_checked"
    assert e.handle(me, "finance_summary", {}, "chat-not-a-uuid")["error"] == "not_allowed"
    assert calls == []


def test_a_blocked_chat_question_gets_the_fixed_reply_and_ends(chat):
    e, _, audits, seen = chat
    me = who(location="local")
    out = e.check_message(me, "her son hurt her", CHAT)
    assert out["stopped"] == "crisis" and out["request_id"] == CHAT
    topic, resp = seen["published"][-1]
    assert resp["guardrail"] == {"stage": "input", "layer": "rules", "category": "crisis"}
    assert e.handle(me, "finance_summary", {}, CHAT)["error"] == "not_allowed"
    assert seen["models"] == []


def test_an_answer_the_output_check_blocks_is_withheld(chat):
    e, _, _, seen = chat
    e._output_check = lambda q, a: GuardrailDecision(False, "crisis", "fake")
    me = who(location="local")
    e.check_message(me, "overtime policy", CHAT)
    out = e.answer(me, CHAT)
    assert out["stopped"] == "output" and "Margins" not in out["reply"]
    assert seen["published"][-1][1]["guardrail"]["stage"] == "output"


def test_chat_requests_expire(chat):
    e, _, _, _ = chat
    now = [0.0]
    e._clock = lambda: now[0]
    me = who(location="local")
    e.check_message(me, "overtime policy", CHAT)
    now[0] = ep.CHAT_SECONDS + 1
    assert e.answer(me, CHAT)["error"] == "not_allowed"


def test_a_stopped_tool_call_ends_the_chat_request(chat):
    e, _, _, seen = chat
    me = who(location="local")
    e.check_message(me, "overtime policy", CHAT)
    out = e.handle(me, "search_knowledge", {"query": "she was hurt"}, CHAT)
    assert out["stopped"] == "crisis"
    assert seen["published"][-1][1]["guardrail"]["category"] == "crisis"
    assert e.answer(me, CHAT)["error"] == "not_allowed"



# --- the organization's administrator decides about outside assistants ---------------------------

def test_outside_assistants_get_nothing_unless_the_administrator_allows_it(endpoint):
    e, calls, audits = endpoint
    out = e.handle(who(allowed=False), "finance_summary", {})
    assert out["error"] == "not_allowed" and "administrator" in out["message"] and calls == []
    assert audits[-1][1]["reason"] == "outside AI not allowed by the organization"
    e.handle(who(location="local", allowed=False), "finance_summary", {})          # inside: unaffected
    assert len(calls) == 1
    e.handle(who(allowed=True), "finance_summary", {})
    assert len(calls) == 2


def test_the_answering_model_is_told_what_the_role_cannot_see(platform):
    p = load_profile_from(platform)
    limits = ep.role_limits(p, "billing")
    assert "finance_summary" in limits and "visit_costs" in limits and "search_knowledge" not in limits
    assert "Never say the data does not exist" in limits
    assert ep.role_limits(p, "finance_analyst") == ""                         # sees every tool
