"""Signed tokens at the governed gateway (ADR 042): a caller is who the identity provider's signature says,
the role comes from the token's groups through the profile's map, and the audit names both the software
and the person it acts for."""

import asyncio
import time
from dataclasses import replace

import jwt
import pytest

from gateway import endpoint as ep
from gateway import gateway
from profiles import loader
from profiles.loader import ProfileError, load_profile
from tests.test_mcp_live import Recorder, free_port, running
from tests.test_sign_in import ISSUER, Provider

AUDIENCE = "governed-gateway"
ROLES = (("clinicians", "clinician"), ("front-desk", "front_desk"))


def settings(**changes):
    base = replace(load_profile("clinic-oracle").outside, auth_kind="jwt", issuer=ISSUER, audience=AUDIENCE, roles=ROLES)
    return replace(base, **changes)


def verifier(provider=None, **changes):
    provider = provider or Provider()
    s = settings(**changes)
    return ep.TokenVerifier(s, ep.OidcVerifier(s.issuer, s.audience, provider.fetch)), provider


def token(provider, **claims):
    now = int(time.time())
    body = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, "sub": "dr.okafor", "azp": "chart-assistant",
            "groups": ["clinicians"], **claims}
    return jwt.encode(body, provider.keys["k1"], algorithm="RS256", headers={"kid": "k1"})


def test_a_signed_token_gives_the_agent_the_person_and_the_role_from_its_groups():
    v, p = verifier()
    who = v.identify(token(p, name="Dr Okafor", tenant="north"))
    assert (who.client_id, who.on_behalf_of, who.role, who.tenant, who.location) == (
        "chart-assistant", "dr.okafor", "clinician", "north", "local")
    assert who.name == "Dr Okafor" and who.external_model_allowed is False
    assert v.identify(token(p, groups=["front-desk", "clinicians"])).role == "clinician"      # the map's order wins
    assert v.identify(token(p, groups=["visitors"])).role is None                           # no mapped group: no role


def test_software_acting_for_itself_has_no_person():
    v, p = verifier()
    assert v.identify(token(p, sub="chart-assistant")).on_behalf_of is None


@pytest.mark.parametrize("claims", [{"exp": int(time.time()) - 3600}, {"aud": "another-service", "azp": "x"},
                                    {"iss": "https://elsewhere.example"}, {"azp": ""}])
def test_a_token_that_does_not_verify_is_no_caller(claims):
    v, p = verifier()
    assert v.identify(token(p, **claims)) is None


def test_forged_unsigned_and_missing_tokens_are_no_caller():
    v, p = verifier()
    other = Provider()                                                     # another key under the same key ID
    assert v.identify(token(other)) is None
    now = int(time.time())
    body = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, "sub": "x", "azp": "y", "groups": ["clinicians"]}
    assert v.identify(jwt.encode(body, None, algorithm="none")) is None
    assert v.identify(None) is None and v.identify("") is None and v.identify("not a token") is None


def test_the_claim_names_and_location_come_from_the_profile():
    v, p = verifier(agent_claim="client_id", person_claim="preferred_username", groups_claim="realm_access.roles",
                    tenant_claim="org", location="external")
    who = v.identify(token(p, client_id="vendor-app", preferred_username="nurse.lee", org="east",
                           realm_access={"roles": ["front-desk"]}, groups=[]))
    assert (who.client_id, who.on_behalf_of, who.role, who.tenant, who.location) == (
        "vendor-app", "nurse.lee", "front_desk", "east", "external")


# ---- the audit names both

def test_every_endpoint_event_and_tool_call_names_the_agent_and_the_person(monkeypatch):
    v, p = verifier()
    who = v.identify(token(p))
    events, calls = [], []
    e = ep.Endpoint(load_profile("clinic-oracle"), audit=lambda topic, event: events.append(event),
                    call=lambda *a, **kw: calls.append(kw) or {"results": []}, mask=lambda t: t,
                    check=lambda text, profile: ("allow", type("D", (), {"allowed": True, "category": None, "reason": None})(), "rules"))
    event = e._event(who, "r1", tool="search_records", decision="allow")
    assert event["client"] == "chart-assistant" and event["on_behalf_of"] == "dr.okafor"
    monkeypatch.setattr(ep, "allowed_tools", lambda profile, role: ["search_records"])
    e.handle(who, "search_records", {"query": "the flu clinic hours"})
    assert calls and calls[0]["client"] == "chart-assistant" and calls[0]["on_behalf_of"] == "dr.okafor"
    alone = replace(who, on_behalf_of=None)
    assert "on_behalf_of" not in e._event(alone, "r2")                    # nothing invented when there is no person


def test_the_tool_call_audit_records_the_person(monkeypatch):
    written = []
    monkeypatch.setattr(gateway, "_audit", written.append)
    monkeypatch.setattr(gateway, "decide", lambda *a, **k: False)
    with pytest.raises(gateway.ToolCallDenied):
        gateway.call_tool("outside", "search_records", {"query": "x"}, "r1", load_profile("clinic-oracle"),
                          audit_args={"query": "x"}, role="clinician", client="chart-assistant", on_behalf_of="dr.okafor")
    assert written[0]["client"] == "chart-assistant" and written[0]["on_behalf_of"] == "dr.okafor"


def test_the_token_check_reports_a_person_too():
    class Reply:
        status_code = 200

        def json(self):
            return {"active": True, "client_id": "app-1", "role": "nurse", "location": "local", "on_behalf_of": "nurse.lee"}
    who = ep.Introspector("http://idp/introspect", "s", 30, post=lambda *a, **k: Reply()).identify("tok")
    assert who.client_id == "app-1" and who.on_behalf_of == "nurse.lee"


# ---- settings

def outside_yaml(auth: dict) -> dict:
    return {"agent": "outside", "auth": auth}


def load_outside(auth):
    profile = load_profile("clinic-oracle")
    return loader._outside(outside_yaml(auth), profile.tools, profile.agents, "outside.yaml")


def test_signed_token_settings_are_read_and_checked():
    o = load_outside({"kind": "jwt", "issuer": "https://login.example.org/", "audience": AUDIENCE,
                      "roles": {"clinicians": "clinician"}, "location": "external"})
    assert (o.auth_kind, o.issuer, o.audience, o.roles, o.location) == (
        "jwt", "https://login.example.org", AUDIENCE, (("clinicians", "clinician"),), "external")
    assert o.identity_url == "https://login.example.org" and load_profile("clinic-oracle").outside.identity_url.endswith("/introspect")
    for bad in ({"kind": "jwt", "audience": AUDIENCE, "roles": {"a": "clinician"}},                     # no issuer
                {"kind": "jwt", "issuer": "login.example.org", "audience": AUDIENCE, "roles": {"a": "clinician"}},
                {"kind": "jwt", "issuer": "https://x.example", "audience": AUDIENCE, "roles": {}},
                {"kind": "jwt", "issuer": "https://x.example", "audience": AUDIENCE, "roles": {"a": "clinician"}, "location": "moon"},
                {"kind": "password", "url": "http://x"}):
        with pytest.raises(ProfileError):
            load_outside(bad)


def test_the_caller_check_is_chosen_by_the_profile():
    assert isinstance(ep.identifier_for(settings(), {}), ep.TokenVerifier)
    o = load_profile("clinic-oracle").outside
    assert isinstance(ep.identifier_for(o, {o.secret_env: "s"}), ep.Introspector)
    with pytest.raises(SystemExit, match=o.secret_env):
        ep.identifier_for(o, {})


# ---- over a real connection

def test_an_assistant_with_a_signed_token_reaches_its_roles_tools(monkeypatch):
    from mcp import types

    from gateway.transport import mcp_session, tool_text
    v, p = verifier()
    profile = load_profile("clinic-oracle")
    catalogue = {n: types.Tool(name=n, description=n, input_schema={"type": "object", "properties": {"query": {"type": "string"}}})
                 for n in ("search_records", "medication_list")}
    monkeypatch.setattr(ep, "allowed_tools", lambda pr, role: ["search_records", "medication_list"] if role == "clinician" else ["search_records"])
    monkeypatch.setattr(ep, "_audit", lambda topic, event: None)

    class Asked(Recorder):
        def handle(self, who, tool, arguments, request_id=None):
            self.calls.append((who.client_id, who.on_behalf_of, who.role, tool))
            return {"results": []}
    recorder = Asked()

    async def ask(tok):
        async with mcp_session(url, headers={"Authorization": f"Bearer {tok}"}) as session:
            names = [t.name for t in (await session.list_tools()).tools]
            tool_text(await session.call_tool("search_records", {"query": "q"}))
            return names
    with running(ep.build_app(profile, v, catalogue, recorder), free_port()) as url:
        assert asyncio.run(ask(token(p))) == ["search_records", "medication_list"]
        assert asyncio.run(ask(token(p, sub="desk.kim", groups=["front-desk"]))) == ["search_records"]
        with pytest.raises(BaseException):  # noqa: B017, PT011 - a refused token surfaces as the library's connection error
            asyncio.run(ask(token(p, exp=int(time.time()) - 60)))
    assert recorder.calls == [("chart-assistant", "dr.okafor", "clinician", "search_records"),
                              ("chart-assistant", "desk.kim", "front_desk", "search_records")]


# ---- the classifier, turned on for a trial without editing a profile

def test_the_environment_can_name_the_text_model_and_its_mode():
    raw = {"text_model": "from-profile", "text_model_mode": "enforce"}
    assert loader._text_model_choice(raw, {}, "p") == ("from-profile", "enforce")
    assert loader._text_model_choice({}, {}, "p") == (None, "observe")
    assert loader._text_model_choice({}, {"GOVERNED_TEXT_MODEL": "sensitivity-modernbert-sens1-r2"}, "p") == (
        "sensitivity-modernbert-sens1-r2", "observe")
    assert loader._text_model_choice(raw, {"GOVERNED_TEXT_MODEL": "none", "GOVERNED_TEXT_MODEL_MODE": "observe"}, "p") == (None, "observe")
    for bad in ({"GOVERNED_TEXT_MODEL": "../elsewhere"}, {"GOVERNED_TEXT_MODEL_MODE": "always"}):
        with pytest.raises(ProfileError):
            loader._text_model_choice({}, bad, "p")


def test_a_committed_clinic_profile_names_no_text_model():
    """Trials turn a classifier on from the environment, which the test setup clears."""
    for name in ("clinic-oracle", "clinic-epic", "clinic-meditech", "clinic-assistant"):
        assert load_profile(name).sensitivity.text_model is None
