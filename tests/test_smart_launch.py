"""Launching from inside the EHR (ADR 035): the launch, the clinician's sign-in, who they are, the
chart's patient, the encrypted sessions, the secrets they rest on, and the API endpoints the EHR calls.

A fake EHR answers with its own signing key. Every refusal the module promises is a test here.
"""

import base64
import json
import stat
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from fhir import secrets as sec
from fhir.client import FhirClient, FhirError
from fhir.sessions import LaunchSession, SessionStore
from fhir.smart_launch import LaunchAuth, Launcher, LaunchSettings, settings_from, user_type
from profiles.fhir_map import FhirMappingError

ISS = "https://ehr.example.test/fhir/r4"
AUTH = "https://ehr.example.test/oauth2/authorize"
TOKEN = "https://ehr.example.test/oauth2/token"
JWKS = "https://ehr.example.test/oauth2/jwks"
CLIENT = "gaai-launch-app"
REDIRECT = "http://localhost:8000/smart/callback"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


EHR_KEY, OTHER_KEY = rsa_key(), rsa_key()


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeEhr:
    """The EHR's SMART configuration, keys and token endpoint."""

    def __init__(self, **token_extra):
        self.posts, self.token_extra, self.claims, self.sign_with, self.header = [], token_extra, {}, EHR_KEY, {}
        self.refresh = {"access_token": "at-2", "expires_in": 3600}

    def get(self, url, headers=None, timeout=None, params=None):
        if url == f"{ISS}/.well-known/smart-configuration":
            return Resp(200, {"authorization_endpoint": AUTH, "token_endpoint": TOKEN, "jwks_uri": JWKS,
                              "issuer": "https://ehr.example.test/oauth2",
                              "capabilities": ["launch-ehr", "client-confidential-symmetric"]})
        if url == JWKS:
            jwk = json.loads(RSAAlgorithm.to_jwk(EHR_KEY.public_key()))
            return Resp(200, {"keys": [{**jwk, "kid": "ehr-1", "use": "sig", "alg": "RS256"}]})
        return Resp(404, {})

    def id_token(self, nonce):
        now = int(time.time())
        claims = {"iss": "https://ehr.example.test/oauth2", "sub": "u-77", "aud": CLIENT, "iat": now,
                  "exp": now + 300, "nonce": nonce, "fhirUser": f"{ISS}/Practitioner/prac-123",
                  "name": "Dr. Test Clinician", **self.claims}
        return jwt.encode(claims, self.sign_with, algorithm="RS256", headers={"kid": "ehr-1", **self.header})

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        if data.get("grant_type") == "refresh_token":
            return Resp(200, self.refresh)
        body = {"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600, "patient": "pat-9001",
                "encounter": "enc-1", "scope": "launch openid fhirUser user/Patient.read",
                "refresh_token": "rt-1", "id_token": self.id_token(self.nonce)}
        body.update(self.token_extra)
        return Resp(200, {k: v for k, v in body.items() if v is not None})


def settings(**kw):
    base = dict(issuers=(ISS,), client_id=CLIENT, redirect_uri=REDIRECT,
                scope="launch openid fhirUser user/Patient.read user/Observation.read",
                roles={"Practitioner": "clinician"}, client_secret="s3cret")
    return LaunchSettings(**{**base, **kw})


def launched(ehr=None, store=None, clock=time.time, **kw):
    ehr = ehr or FakeEhr()
    store = store or SessionStore(SessionStore.new_key(), clock=clock)
    launcher = Launcher(settings(**kw), store, ehr, clock)
    url = launcher.start(ISS, "launch-abc")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    ehr.nonce = q["nonce"]
    return launcher, ehr, q


# --- step 1: the launch ------------------------------------------------------------------------------------

def test_the_launch_sends_the_browser_to_the_ehrs_sign_in_with_the_launch_code():
    launcher, ehr, q = launched()
    assert q["launch"] == "launch-abc" and q["aud"] == ISS and q["client_id"] == CLIENT
    assert q["redirect_uri"] == REDIRECT and q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["scope"].split()[:3] == ["launch", "openid", "fhirUser"] and len(q["state"]) > 30 and q["nonce"]


@pytest.mark.parametrize("iss", ["https://evil.example/fhir", "https://ehr.example.test/fhir/r4/../other", ""])
def test_a_launch_from_an_ehr_not_configured_is_refused(iss):
    launcher = Launcher(settings(), SessionStore(SessionStore.new_key()), FakeEhr())
    with pytest.raises(FhirError, match="not configured"):
        launcher.start(iss, "launch-abc")


# --- step 2: the callback ------------------------------------------------------------------------------------

def test_the_callback_makes_a_session_for_the_clinician_on_the_charts_patient():
    launcher, ehr, q = launched()
    s = launcher.finish("code-1", q["state"], browser_state=q["state"])
    assert (s.patient, s.role, s.user, s.name) == ("pat-9001", "clinician", f"{ISS}/Practitioner/prac-123",
                                                   "Dr. Test Clinician")
    url, form, headers = ehr.posts[0]
    assert url == TOKEN and form["code_verifier"] and form["redirect_uri"] == REDIRECT and "client_id" not in form
    assert headers["Authorization"] == "Basic " + base64.b64encode(f"{CLIENT}:s3cret".encode()).decode()
    assert launcher.session(s.id).access_token == "at-1"


def test_a_state_is_used_once_and_expires():
    launcher, ehr, q = launched()
    launcher.finish("code-1", q["state"], browser_state=q["state"])
    with pytest.raises(FhirError, match="unknown or has expired"):
        launcher.finish("code-1", q["state"], browser_state=q["state"])
    now = [1000.0]
    launcher2, _, q2 = launched(clock=lambda: now[0])
    now[0] += 601
    with pytest.raises(FhirError, match="expired"):
        launcher2.finish("code-1", q2["state"], browser_state=q2["state"])


def test_an_error_from_the_ehr_or_a_missing_code_ends_the_sign_in():
    launcher, _, q = launched()
    with pytest.raises(FhirError, match="access_denied"):
        launcher.finish(None, q["state"], "access_denied", q["state"])
    launcher, _, q = launched()
    with pytest.raises(FhirError, match="no code"):
        launcher.finish("", q["state"], browser_state=q["state"])


@pytest.mark.parametrize("change, message", [
    ({"sign_with": OTHER_KEY}, "not accepted"),                       # not the EHR's key
    ({"claims": {"aud": "another-app"}}, "not accepted"),             # for another app
    ({"claims": {"exp": int(time.time()) - 3600}}, "not accepted"),   # expired
    ({"claims": {"iss": "https://elsewhere.example"}}, "not accepted"),
    ({"claims": {"nonce": "replayed"}}, "nonce"),
    ({"header": {"kid": "unknown-key"}}, "does not publish"),
])
def test_an_id_token_the_ehr_did_not_issue_for_this_sign_in_is_refused(change, message):
    launcher, ehr, q = launched()
    for k, v in change.items():
        setattr(ehr, k, v)
    with pytest.raises(FhirError, match=message):
        launcher.finish("code-1", q["state"], browser_state=q["state"])


def test_an_unsigned_id_token_is_refused():
    launcher, ehr, q = launched()
    ehr.id_token = lambda nonce: jwt.encode({"aud": CLIENT, "fhirUser": "Practitioner/1"}, None, algorithm="none")
    with pytest.raises(FhirError, match="algorithm"):
        launcher.finish("code-1", q["state"], browser_state=q["state"])


@pytest.mark.parametrize("extra, message", [
    ({"patient": None}, "no patient"),
    ({"id_token": None}, "no ID token"),
])
def test_a_launch_without_a_patient_or_without_who_signed_in_is_refused(extra, message):
    launcher, ehr, q = launched(FakeEhr(**extra))
    with pytest.raises(FhirError, match=message):
        launcher.finish("code-1", q["state"], browser_state=q["state"])


def test_a_user_type_with_no_role_here_is_refused():
    launcher, ehr, q = launched()
    ehr.claims = {"fhirUser": f"{ISS}/Patient/pat-9001"}
    with pytest.raises(FhirError, match="a Patient has no role"):
        launcher.finish("code-1", q["state"], browser_state=q["state"])
    assert user_type("Practitioner/1") == "Practitioner" and user_type(f"{ISS}/RelatedPerson/7") == "RelatedPerson"


def test_an_asymmetric_client_signs_an_assertion_for_the_token_endpoint():
    key = rsa_key()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    launcher, ehr, q = launched(client_secret=None, private_key_pem=pem, key_id="gaai-1")
    launcher.finish("code-1", q["state"], browser_state=q["state"])
    _, form, headers = ehr.posts[0]
    claims = jwt.decode(form["client_assertion"], key.public_key(), algorithms=["RS384"], audience=TOKEN)
    assert claims["iss"] == claims["sub"] == CLIENT and "Authorization" not in headers
    launcher, ehr, q = launched(client_secret=None)                   # a public client: PKCE only
    launcher.finish("code-1", q["state"], browser_state=q["state"])
    assert ehr.posts[0][1]["client_id"] == CLIENT


# --- the session ------------------------------------------------------------------------------------------------

def test_the_session_refreshes_its_token_and_ends_on_time():
    now = [time.time()]
    launcher, ehr, q = launched(clock=lambda: now[0])
    s = launcher.finish("code-1", q["state"], browser_state=q["state"])
    now[0] += 3600
    assert launcher.session(s.id).access_token == "at-2"
    assert ehr.posts[-1][1] == {"grant_type": "refresh_token", "refresh_token": "rt-1"}
    now[0] += 8 * 3600
    assert launcher.session(s.id) is None                              # the session's own end
    launcher.end(s.id)


def test_without_a_refresh_token_an_ended_token_ends_the_session():
    now = [time.time()]
    launcher, ehr, q = launched(FakeEhr(refresh_token=None), clock=lambda: now[0])
    s = launcher.finish("code-1", q["state"], browser_state=q["state"])
    now[0] += 3601
    assert launcher.session(s.id) is None


def test_sessions_are_encrypted_at_rest_named_by_hash_and_owner_only(tmp_path):
    store = SessionStore(SessionStore.new_key(), tmp_path / "sessions")
    launcher, ehr, q = launched(store=store)
    s = launcher.finish("code-1", q["state"], browser_state=q["state"])
    files = list((tmp_path / "sessions").iterdir())
    assert len(files) == 1 and s.id not in files[0].name
    raw = files[0].read_bytes()
    assert b"at-1" not in raw and b"pat-9001" not in raw and b"Practitioner" not in raw
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "sessions").stat().st_mode) == 0o700
    assert SessionStore(SessionStore.new_key(), tmp_path / "sessions").load(s.id) is None   # another key
    assert store.load(s.id + "x") is None


def test_the_record_tools_read_with_the_clinicians_token_and_the_charts_patient():
    store = SessionStore(SessionStore.new_key())
    launcher, ehr, q = launched(store=store)
    s = launcher.finish("code-1", q["state"], browser_state=q["state"])
    auth = LaunchAuth(store, s.id)
    seen = []

    class Fhir:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.append(headers["Authorization"])
            r = Resp(200, {"resourceType": "Patient", "id": "pat-9001"})
            r.headers = {}
            return r
    assert FhirClient(ISS, auth, Fhir()).read("Patient", auth.patient)["id"] == "pat-9001"
    assert seen == ["Bearer at-1"]
    launcher.end(s.id)
    with pytest.raises(FhirError, match="ended"):
        auth.token()


# --- the profile's launch settings ------------------------------------------------------------------------------

def mapping(launch):
    import yaml

    from profiles.fhir_map import parse
    from profiles.loader import load_profile
    p = load_profile("clinic-oracle")
    raw = yaml.safe_load((p.path / "fhir.yaml").read_text())
    raw["launch"] = launch
    return parse(raw, "fhir.yaml", set(p.data_classes))


GOOD = {"client_id_env": "ORACLE_LAUNCH_CLIENT_ID", "client_secret_ref": "env:ORACLE_LAUNCH_SECRET",
        "redirect_uri": REDIRECT, "scope": "launch openid fhirUser user/Patient.read user/Observation.read",
        "roles": {"Practitioner": "clinician"}}


def test_a_launch_block_is_read_and_defaults_to_the_profiles_server():
    m = mapping(GOOD)
    assert m.launch["issuers"] == [m.server["base_url"]]
    s = settings_from(m.launch, {"ORACLE_LAUNCH_CLIENT_ID": "app-1", "ORACLE_LAUNCH_SECRET": "x"})
    assert s.client_id == "app-1" and s.client_secret == "x" and s.roles == {"Practitioner": "clinician"}
    with pytest.raises(FhirError, match="ORACLE_LAUNCH_CLIENT_ID"):
        settings_from(m.launch, {})


@pytest.mark.parametrize("change, message", [
    ({"scope": "launch openid fhirUser user/Patient.write"}, "no write"),
    ({"scope": "launch openid fhirUser system/Patient.read"}, "no write and no system"),
    ({"scope": "launch openid user/Patient.read"}, "fhirUser"),
    ({"redirect_uri": "http://app.example.com/smart/callback"}, "https"),
    ({"client_secret": "abc"}, "never its value"),
    ({"client_secret_ref": "vault:abc"}, "a secret is named as"),
    ({"client_secret_ref": "azure-keyvault:https://evil.example/secrets/x"}, "vault.azure.net"),
    ({"roles": {}}, "roles maps"),
    ({"issuers": ["http://ehr.example/fhir"]}, "https"),
])
def test_unsafe_launch_settings_are_refused(change, message):
    with pytest.raises(FhirMappingError, match=message):
        mapping({**GOOD, **change})


# --- secrets ------------------------------------------------------------------------------------------------------

def test_secrets_from_the_environment_a_private_file_and_the_two_vaults(tmp_path):
    assert sec.get("env:X", {"X": "1"}) == "1"
    f = tmp_path / "s.txt"
    f.write_text("from-file\n")
    f.chmod(0o644)
    with pytest.raises(sec.SecretError, match="can be read by others"):
        sec.get(f"file:{f}")
    f.chmod(0o600)
    assert sec.get(f"file:{f}") == "from-file"
    calls = []
    fetchers = {"azure-keyvault": lambda u: calls.append(u) or "kv-value",
                "aws-secretsmanager": lambda n: calls.append(n) or "sm-value"}
    kv = "azure-keyvault:https://gaai-dev.vault.azure.net/secrets/epic-client-secret"
    now = [0.0]
    assert sec.get(kv, fetchers=fetchers, clock=lambda: now[0]) == "kv-value"
    assert sec.get(kv, fetchers=fetchers, clock=lambda: now[0]) == "kv-value" and len(calls) == 1   # cached
    now[0] += 301
    sec.get(kv, fetchers=fetchers, clock=lambda: now[0])
    assert len(calls) == 2                                              # a rotated secret is picked up
    assert sec.get("aws-secretsmanager:epic-key", fetchers=fetchers) == "sm-value"


def test_the_session_key_is_made_once_and_kept_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    k1 = sec.session_key(environ={})
    k2 = sec.session_key(environ={})
    p = tmp_path / ".config/governed-agentic-ai/session.key"
    assert k1 == k2 and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert sec.session_key(environ={"GAAI_SESSION_KEY_REF": "env:K", "K": "abc"}) == b"abc"


# --- the API endpoints the EHR calls ---------------------------------------------------------------------------------

def test_the_endpoints_launch_set_a_private_cookie_and_show_the_session(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api import main
    store = SessionStore(SessionStore.new_key())
    ehr = FakeEhr()
    launcher = Launcher(settings(roles={"Practitioner": "clinician"}), store, ehr)
    monkeypatch.setattr(main, "LAUNCHER", launcher)
    client = TestClient(main.app, base_url="https://testserver")
    r = client.get("/smart/launch", params={"iss": ISS, "launch": "L-1"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(AUTH)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    ehr.nonce = q["nonce"]
    assert client.get("/smart/launch", params={"iss": "https://evil.example", "launch": "x"}).status_code == 400
    r = client.get("/smart/callback", params={"code": "c-1", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/?launched=1"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=none" in cookie and "at-1" not in cookie
    info = client.get("/smart/session").json()
    assert info["launched"] and info["patient"] == "pat-9001" and info["role"] == "clinician"
    assert "at-1" not in json.dumps(info) and "access_token" not in info
    assert client.get("/smart/callback", params={"code": "c-1", "state": q["state"]}).status_code == 400
    client.post("/smart/end")
    assert client.get("/smart/session").json() == {"launched": False}
    # a callback link opened in a browser that did not start the sign-in (login CSRF) is refused
    r = client.get("/smart/launch", params={"iss": ISS, "launch": "L-2"}, follow_redirects=False)
    q2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    other = TestClient(main.app, base_url="https://testserver")
    assert other.get("/smart/callback", params={"code": "c-2", "state": q2["state"]}).status_code == 400


def test_a_session_object_holds_what_the_tools_need():
    fields = set(LaunchSession.__dataclass_fields__)
    assert {"patient", "role", "user", "access_token", "ends_at"} <= fields


# --- the launch through the platform -----------------------------------------------------------------------

def chart_session(store=None, iss=None):
    """A session as the API's callback leaves it, and its reference."""
    from profiles.loader import load_profile
    store = store or SessionStore(SessionStore.new_key())
    oracle = load_profile("clinic-oracle").fhir_map
    s = LaunchSession(id="sid-" + "x" * 40, iss=iss or oracle.server["base_url"], patient="90000001",
                      encounter=None, user="Practitioner/prac-1", name="Dr. Test", role="clinician",
                      access_token="at-chart", refresh_token=None, expires_at=time.time() + 3600, scope="",
                      token_endpoint=TOKEN, created=time.time(), ends_at=time.time() + 3600)
    store.save(s)
    return store, store.ref(s.id), s


def oracle_with_launch():
    from dataclasses import replace

    from profiles.loader import load_profile
    p = load_profile("clinic-oracle")
    return p, replace(p.fhir_map, launch={**GOOD, "issuers": [p.fhir_map.server["base_url"]]})


def test_the_records_tool_uses_the_charts_patient_without_a_name_and_refuses_another():
    from test_fhir_connectors import PATIENT, FakeFhir

    from mcp_servers.fhir_records.server import find_patient
    p, m = oracle_with_launch()
    store, ref, s = chart_session()
    fake = FakeFhir(m.server["base_url"])
    from fhir.smart_launch import launch_client
    c = launch_client(m, ref, p.name, store=store, session=fake)
    got, why = find_patient(c, m, "Give me a brief on this patient")
    assert got["id"] == PATIENT["id"] and why is None
    assert [g[0] for g in fake.gets] == ["/Patient/90000001"]            # read, never searched
    assert fake.gets[0][2]["Authorization"] == "Bearer at-chart"         # the clinician's own token
    assert find_patient(c, m, "brief on Ines Testarossa born 1971-03-04")[0]["id"] == "90000001"
    assert find_patient(c, m, "and the patient born 1950-02-02?") == (None, "other patient")


def test_a_session_ref_from_another_ehr_or_an_ended_session_is_refused():
    from fhir.smart_launch import launch_client
    p, m = oracle_with_launch()
    store, ref, _ = chart_session(iss="https://other-ehr.example/fhir")
    with pytest.raises(FhirError, match="does not accept"):
        launch_client(m, ref, p.name, store=store)
    with pytest.raises(FhirError, match="ended"):
        launch_client(m, "0" * 64, p.name, store=store)
    assert store.load_ref("not-a-ref/../../x") is None


def test_the_chart_brief_runs_on_the_charts_patient(monkeypatch):
    from test_fhir_connectors import ChartServer

    import mcp_servers.chart_brief.server as cb
    import mcp_servers.medication_review.server as mr
    from fhir.smart_launch import launch_client
    monkeypatch.setattr(mr, "fetch_missing", lambda cfg, meds, lookup=None: [])
    p, m = oracle_with_launch()
    store, ref, _ = chart_session()
    c = launch_client(m, ref, p.name, store=store, session=ChartServer(m.server["base_url"]))
    answer = cb.brief(None, p, cb.load_config(p), "Give me a brief", list(p.data_classes), c)["answer"]
    assert answer.startswith("Chart brief for Ines M Testarossa") and "Captopril" in answer


def test_the_gateway_gives_record_tools_the_launch_and_a_caller_cannot_forge_it(monkeypatch):
    from gateway import gateway
    from profiles.loader import load_profile
    clinic = load_profile("clinic-oracle")
    events, sent = [], []
    monkeypatch.setattr(gateway, "_is_allowed", lambda *a, **k: True)
    monkeypatch.setattr(gateway, "allowed_classes", lambda p, r: ["medications"])
    monkeypatch.setattr(gateway, "_audit", events.append)

    async def fake_call(url, tool, args, on_progress=None, timeout=None):
        sent.append(dict(args))
        return {"results": []}
    monkeypatch.setattr(gateway, "_call", fake_call)
    forged = {"query": "x", "launch_ref": "f" * 64}
    gateway.call_tool("planner", "search_records", forged, "r1", clinic, audit_args={"query": "x"}, role="clinician")
    assert "launch_ref" not in sent[-1]                                  # a caller's own value is dropped
    gateway.call_tool("planner", "search_records", {"query": "x"}, "r2", clinic, audit_args={"query": "x"},
                      role="clinician", launch="a" * 64)
    assert sent[-1]["launch_ref"] == "a" * 64
    assert events[-1]["launched"] is True and "a" * 64 not in json.dumps(events[-1])   # not in the audit
    gateway.call_tool("planner", "search_knowledge", {"query": "x"}, "r3", clinic, role="clinician", launch="a" * 64)
    assert "launch_ref" not in sent[-1]                                  # only record tools get it


def test_the_endpoint_never_takes_a_launch_ref_from_outside():
    from gateway.endpoint import HIDDEN_PARAMS
    assert "launch_ref" in HIDDEN_PARAMS


def test_a_sign_in_started_in_another_browser_cannot_be_finished_here():
    """Login CSRF: someone starts a launch and sends their callback link to a clinician."""
    launcher, ehr, q = launched()
    with pytest.raises(FhirError, match="not started in this browser"):
        launcher.finish("attackers-code", q["state"], browser_state="")
    with pytest.raises(FhirError, match="unknown"):                      # and the state is now spent
        launcher.finish("code-1", q["state"], browser_state=q["state"])


def test_a_flood_of_launches_is_capped(monkeypatch):
    from fhir import smart_launch
    monkeypatch.setattr(smart_launch, "MAX_PENDING", 3)
    launcher = Launcher(settings(), SessionStore(SessionStore.new_key()), FakeEhr())
    for _ in range(3):
        launcher.start(ISS, "L")
    with pytest.raises(FhirError, match="too many"):
        launcher.start(ISS, "L")
