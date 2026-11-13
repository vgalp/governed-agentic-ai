"""Oracle Health and MEDITECH connectors (ADR 032): the open sign-in, the client secret, sign-in by a
person (authorization code with PKCE, refresh tokens in an owner-only file), the patient a sign-in is
for, and the two profiles.

No network: fake servers answer in the shape Oracle Health and MEDITECH (US Core) return.
Invented test data only: not the vendors' test patients, not real people.
"""

import base64
import hashlib
import json
import socket
import stat
import threading
import time
import urllib.parse
import urllib.request

import pytest

from fhir.client import FhirClient, FhirError, SmartBackendAuth, SmartBackendSettings, client_from_settings
from fhir.smart_user import SmartUserAuth, SmartUserSettings, TokenStore, authorize_url, exchange_code, pkce_pair
from mcp_servers.fhir_records.server import find_patient, search
from profiles.fhir_map import FhirMappingError, parse
from profiles.loader import load_profile

ORACLE = load_profile("clinic-oracle")
MEDITECH = load_profile("clinic-meditech")
EPIC = load_profile("clinic-epic")
TOKEN_URL = "https://auth.example.test/oauth/token"


class Response:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def bundle(resources):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}


PATIENT = {"resourceType": "Patient", "id": "90000001", "gender": "female", "birthDate": "1971-03-04",
           "name": [{"use": "official", "family": "Testarossa", "given": ["Ines", "M"]}],
           "address": [{"city": "Kansas City"}]}
MEDS = [{"resourceType": "MedicationRequest", "id": "300000017", "status": "active", "intent": "order",
         "authoredOn": "2026-03-01T10:00:00Z",
         "medicationCodeableConcept": {"text": "metformin 500 MG tablet",
                                       "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                                   "code": "861007"}]}}]


class FakeFhir:
    """Answers GETs from invented resources and token POSTs; records every request."""

    def __init__(self, base, token_body=None):
        self.base, self.gets, self.posts = base, [], []
        self.token_body = token_body or {"access_token": "t-1", "token_type": "Bearer", "expires_in": 570}

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.removeprefix(self.base)
        self.gets.append((path, dict(params or {}), dict(headers or {})))
        if path == "/Patient":
            ok = params.get("family") == "Testarossa" and params.get("birthdate") == "1971-03-04"
            return Response(200, bundle([PATIENT] if ok else []))
        if path == "/Patient/90000001":
            return Response(200, PATIENT)
        if path == "/MedicationRequest":
            return Response(200, bundle(MEDS))
        if path in ("/Encounter", "/Condition", "/AllergyIntolerance", "/Observation"):
            return Response(200, bundle([]))
        return Response(404, {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "not found"}]})

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        return Response(200, dict(self.token_body))


def raw(profile):
    import yaml
    return yaml.safe_load((profile.path / "fhir.yaml").read_text())


# --- Oracle Health: the open sandbox ------------------------------------------------------------------

def test_the_oracle_profile_reads_the_open_sandbox_without_signing_in():
    m = ORACLE.fhir_map
    assert m.server["auth"] == "none" and "fhir-open.cerner.com" in m.server["base_url"]
    fake = FakeFhir(m.server["base_url"])
    c = client_from_settings(m.server, environ={}, session=fake)
    out = search(c, m, "What medications is Ines Testarossa (born 1971-03-04) taking?", ["patient_details",
                                                                                          "medications"])
    assert [r["id"] for r in out["results"]] == ["pt-90000001", "med-00000017"]
    assert all("Authorization" not in h for _, _, h in fake.gets) and fake.posts == []


def test_oracle_numeric_ids_make_source_ids_the_profile_accepts():
    pattern = ORACLE.source_id_pattern
    for rid in ("pt-90000001", "med-00000017", "cond-p7001", "vit-12345678"):
        assert pattern.search(f"[{rid}]"), rid


def test_a_system_account_can_sign_in_with_its_secret_instead_of_a_key():
    fake = FakeFhir("https://fhir.example.test/r4/t")
    auth = SmartBackendAuth(SmartBackendSettings(TOKEN_URL, "acct-1", b"", None, "system/Patient.read", "s3cret"),
                            fake)
    assert auth.token() == "t-1"
    url, form, headers = fake.posts[0]
    assert form == {"grant_type": "client_credentials", "scope": "system/Patient.read"}   # no assertion
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"acct-1:s3cret").decode()


def test_the_secret_comes_from_the_environment():
    server = {"base_url": "https://fhir.example.test/r4/t", "auth": "smart_backend", "token_url": TOKEN_URL,
              "client_id_env": "O_ID", "client_secret_env": "O_SECRET"}
    with pytest.raises(FhirError, match="O_SECRET"):
        client_from_settings(server, environ={"O_ID": "acct-1"})
    c = client_from_settings(server, environ={"O_ID": "acct-1", "O_SECRET": "s"}, session=FakeFhir(""))
    assert c.auth.s.client_secret == "s" and c.auth.s.private_key_pem == b""


# --- the mapping refuses unsafe sign-in settings ------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    ({"auth": "smart_backend", "token_url": TOKEN_URL, "client_id_env": "X"}, "private_key_env"),
    ({"auth": "smart_user", "token_url": TOKEN_URL, "client_id_env": "X", "token_store": "data/token.json"},
     "outside the project"),
    ({"auth": "smart_user", "token_url": TOKEN_URL, "client_id_env": "X"}, "token_store"),
    ({"auth": "smart_user", "token_url": "http://auth.example.test/token", "client_id_env": "X",
      "token_store": "~/t.json"}, "https"),
    ({"client_secret": "abc"}, r"\.env"),
    ({"refresh_token": "abc"}, r"\.env"),
])
def test_the_mapping_refuses_unsafe_sign_in_settings(change, message):
    r = raw(MEDITECH)
    r["server"] = {"base_url": "https://fhir.example.test/r4", **change}
    with pytest.raises(FhirMappingError, match=message):
        parse(r, "fhir.yaml", set(MEDITECH.data_classes))


# --- MEDITECH: sign-in by a person -------------------------------------------------------------------------

def settings(tmp_path, secret="sec"):
    return SmartUserSettings(TOKEN_URL, "app-1", secret, str(tmp_path / "cfg" / "token.json"))


def test_pkce_challenge_is_the_s256_of_the_verifier_and_the_link_carries_it():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    link = authorize_url("https://auth.example.test/oauth/authorize", "app-1", "http://localhost:8765/callback",
                         "launch/patient patient/*.read", "https://fhir.example.test/r4", "st-1", challenge)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
    assert q == {"response_type": "code", "client_id": "app-1", "redirect_uri": "http://localhost:8765/callback",
                 "scope": "launch/patient patient/*.read", "state": "st-1", "aud": "https://fhir.example.test/r4",
                 "code_challenge": challenge, "code_challenge_method": "S256"}


def test_the_code_is_traded_once_and_the_tokens_kept_for_the_owner_only(tmp_path):
    fake = FakeFhir("", {"access_token": "a-1", "refresh_token": "r-1", "expires_in": 3600,
                         "scope": "patient/*.read", "patient": "90000001"})
    s = settings(tmp_path)
    kept = exchange_code(s, "code-1", "verifier-1", session=fake, clock=lambda: 1000.0)
    _, form, _ = fake.posts[0]
    assert form == {"grant_type": "authorization_code", "code": "code-1", "redirect_uri": s.redirect_uri,
                    "code_verifier": "verifier-1", "client_id": "app-1", "client_secret": "sec"}
    assert kept["patient"] == "90000001" and kept["expires_at"] == 4600.0
    f = tmp_path / "cfg" / "token.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert json.loads(f.read_text())["refresh_token"] == "r-1"


def test_a_refused_code_keeps_nothing(tmp_path):
    class Refuse(FakeFhir):
        def post(self, url, data=None, headers=None, timeout=None):
            return Response(400, {"error": "invalid_grant", "error_description": "code expired"})
    with pytest.raises(FhirError, match="invalid_grant"):
        exchange_code(settings(tmp_path), "old", "v", session=Refuse(""))
    assert not (tmp_path / "cfg" / "token.json").exists()


def test_without_signing_in_the_client_says_how(tmp_path):
    with pytest.raises(FhirError, match="fhir_connect"):
        SmartUserAuth(settings(tmp_path), session=FakeFhir("")).token()


def test_the_access_token_is_reused_then_refreshed_and_a_new_refresh_token_kept(tmp_path):
    s, now = settings(tmp_path), [1000.0]
    TokenStore(s.token_store).save({"access_token": "a-1", "refresh_token": "r-1", "expires_at": 4600.0,
                                    "patient": "90000001", "scope": "patient/*.read"})
    fake = FakeFhir("", {"access_token": "a-2", "refresh_token": "r-2", "expires_in": 3600})
    auth = SmartUserAuth(s, session=fake, clock=lambda: now[0])
    assert auth.token() == "a-1" and fake.posts == []
    now[0] = 4590.0                                          # within 30 s of the end
    assert auth.token() == "a-2"
    _, form, _ = fake.posts[0]
    assert form == {"grant_type": "refresh_token", "refresh_token": "r-1", "client_id": "app-1",
                    "client_secret": "sec"}
    data = TokenStore(s.token_store).load()
    assert data["refresh_token"] == "r-2" and data["patient"] == "90000001"   # the patient stays


def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(tmp_path):
    s = settings(tmp_path)
    TokenStore(s.token_store).save({"access_token": "a-1", "refresh_token": "r-1", "expires_at": 0})
    SmartUserAuth(s, session=FakeFhir("", {"access_token": "a-2", "expires_in": 60})).token()
    assert TokenStore(s.token_store).load()["refresh_token"] == "r-1"


def test_a_refused_refresh_says_to_sign_in_again(tmp_path):
    class Refuse(FakeFhir):
        def post(self, url, data=None, headers=None, timeout=None):
            return Response(400, {"error": "invalid_grant"})
    s = settings(tmp_path)
    TokenStore(s.token_store).save({"access_token": "a-1", "refresh_token": "r-1", "expires_at": 0})
    with pytest.raises(FhirError, match="invalid_grant.*fhir_connect"):
        SmartUserAuth(s, session=Refuse("")).token()


# --- the patient a sign-in is for ---------------------------------------------------------------------------

def signed_in_client(tmp_path, base):
    s = settings(tmp_path)
    TokenStore(s.token_store).save({"access_token": "a-1", "refresh_token": "r-1", "expires_at": time.time() + 3600,
                                    "patient": "90000001"})
    fake = FakeFhir(base)
    return FhirClient(base, SmartUserAuth(s, session=fake), session=fake), fake


def test_a_one_patient_sign_in_reads_that_patient_and_never_searches(tmp_path):
    m = MEDITECH.fhir_map
    c, fake = signed_in_client(tmp_path, m.server["base_url"])
    p, why = find_patient(c, m, "What medications is Ines Testarossa (born 1971-03-04) taking?")
    assert p["id"] == "90000001" and why is None
    assert [g[0] for g in fake.gets] == ["/Patient/90000001"]
    assert fake.gets[0][2]["Authorization"] == "Bearer a-1"


@pytest.mark.parametrize("question", ["What medications is Ines Testarossa (born 1971-03-05) taking?",
                                      "What medications is Nora Testwell (born 1971-03-04) taking?"])
def test_the_question_must_name_the_signed_in_patient_with_the_date_of_birth(tmp_path, question):
    m = MEDITECH.fhir_map
    c, _ = signed_in_client(tmp_path, m.server["base_url"])
    assert find_patient(c, m, question) == (None, "none")


def test_meditech_asks_us_core_for_orders_with_the_patient(tmp_path):
    m = MEDITECH.fhir_map
    c, fake = signed_in_client(tmp_path, m.server["base_url"])
    out = search(c, m, "Ines Testarossa born 1971-03-04 medications", ["patient_details", "medications"])
    assert [r["id"] for r in out["results"]] == ["pt-90000001", "med-00000017"]
    path, params, _ = fake.gets[-1]
    assert path == "/MedicationRequest" and params["patient"] == "90000001" and params["intent"] == "order"


# --- the profiles -------------------------------------------------------------------------------------------

def test_the_new_profiles_are_the_epic_clinic_with_another_server():
    for p in (ORACLE, MEDITECH):
        assert p.roles == EPIC.roles and p.data_classes == EPIC.data_classes
        assert {r.name for r in p.fhir_map.records} == {r.name for r in EPIC.fhir_map.records}
        text = (p.path / "fhir.yaml").read_text() + (p.path / "profile.yaml").read_text()
        assert "Epic" not in text and "secret:" not in text.replace("client_secret_env:", "")
    assert MEDITECH.fhir_map.server["token_store"].startswith("~/.config/governed-agentic-ai/")


# --- the browser comes back to localhost ---------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("state, expect", [("st-ok", "code-9"), ("st-wrong", "wrong state")])
def test_the_sign_in_waits_for_the_browser_and_checks_the_state(state, expect):
    from scripts.fhir_connect import wait_for_code
    port = free_port()
    uri = f"http://127.0.0.1:{port}/callback"

    def browser():
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{uri}?code=code-9&state={state}", timeout=2).read()
                return
            except OSError:
                time.sleep(0.05)
    threading.Thread(target=browser, daemon=True).start()
    if expect.startswith("code"):
        assert wait_for_code(uri, "st-ok", seconds=10) == expect
    else:
        with pytest.raises(FhirError, match=expect):
            wait_for_code(uri, "st-ok", seconds=10)


def test_the_redirect_must_be_on_this_computer():
    from scripts.fhir_connect import wait_for_code
    with pytest.raises(FhirError, match="localhost"):
        wait_for_code("https://app.example.test/callback", "s", seconds=1)


def test_a_server_that_cannot_be_reached_is_an_error_not_a_crash():
    import requests

    class Down:
        def get(self, url, **kw):
            raise requests.ConnectionError("tunnel failed")
    with pytest.raises(FhirError, match="could not reach fhir.example.test"):
        FhirClient("https://fhir.example.test/r4", None, Down()).capability()


def test_a_timed_out_read_is_tried_once_more():
    import requests

    class Slow:
        calls = 0

        def get(self, url, **kw):
            Slow.calls += 1
            if Slow.calls == 1:
                raise requests.ReadTimeout("slow")
            return Response(200, {"resourceType": "CapabilityStatement"})
    assert FhirClient("https://fhir.example.test/r4", None, Slow()).capability()["resourceType"] == \
        "CapabilityStatement" and Slow.calls == 2


def test_the_timeout_comes_from_the_mapping():
    assert client_from_settings(ORACLE.fhir_map.server, environ={}).timeout == 45


def test_a_name_search_keeps_only_exact_names():
    """FHIR string search matches the start of a name: Wilm also returns Wilma."""
    longer = {**PATIENT, "id": "90000002", "name": [{"family": "Testarossa", "given": ["Inesa"]}]}

    class Prefix(FakeFhir):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/Patient"):
                return Response(200, bundle([PATIENT, longer]))
            return super().get(url, params, headers, timeout)
    m = ORACLE.fhir_map
    c = FhirClient(m.server["base_url"], None, Prefix(m.server["base_url"]))
    p, why = find_patient(c, m, "Ines Testarossa 1971-03-04")
    assert p["id"] == "90000001" and why is None


def test_the_chart_brief_asks_once_per_record_type_and_side_by_side(monkeypatch):
    """A slow sandbox: each search takes 0.3 s. Five record types, read once each and in parallel."""
    import mcp_servers.medication_review.server as mr
    from mcp_servers.chart_brief.server import brief
    monkeypatch.setattr(mr, "fetch_missing", lambda cfg, meds, lookup=None: [])   # no label downloads here
    from mcp_servers.chart_brief.server import load_config as brief_config

    class Slow(FakeFhir):
        def get(self, url, params=None, headers=None, timeout=None):
            if not url.endswith("/Patient") and "/Patient/" not in url:
                time.sleep(0.3)
            return super().get(url, params, headers, timeout)
    m = ORACLE.fhir_map
    fake = Slow(m.server["base_url"])
    c = FhirClient(m.server["base_url"], None, fake)
    start = time.time()
    out = brief(None, ORACLE, brief_config(ORACLE), "Give me a brief on Ines Testarossa, born 1971-03-04",
                list(ORACLE.data_classes), c)
    took = time.time() - start
    assert "Testarossa" in out["answer"]
    searched = [g[0] for g in fake.gets if g[0] != "/Patient"]
    for kind in ("/Encounter", "/Condition", "/AllergyIntolerance", "/Observation"):
        assert searched.count(kind) == 1, (kind, searched)
    assert took < 5 * 0.3 * 2                 # one after another, twice each, would take 3 s or more


# --- the chart brief on a record like the Oracle sandbox's (ADR 023, 032) -----------------------------

def _med(rid, text, when):
    return {"resourceType": "MedicationRequest", "id": rid, "status": "active", "authoredOn": when,
            "medicationCodeableConcept": {"text": text}}


CHART = {
    "/MedicationRequest": [_med("41000001", "Captopril", "2024-11-09"), _med("41000002", "Captopril", "2024-11-10"),
                           _med("41000003", "Captopril", "2024-11-11"),
                           _med("06635049", "Acetaminophen (Tylenol 325 mg oral capsule)", "2023-12-09"),
                           _med("94715677", "Acetaminophen-diphenhydramine (Tylenol PM 1000 mg-50 mg/30 mL oral liquid)",
                                "2023-07-26")],
    "/AllergyIntolerance": [{"resourceType": "AllergyIntolerance", "id": "12774995", "code": {"text": "Allergy to collodion"}},
                            {"resourceType": "AllergyIntolerance", "id": "12781535",
                             "code": {"text": "Isosorbide dinitrate allergy"}}],
    "/Condition": [{"resourceType": "Condition", "id": "76635963", "recordedDate": "2022-10-01",
                    "code": {"text": "Chronic venous hypertension (idiopathic) with ulcer of right lower extremity"}}],
    "/Observation": [{"resourceType": "Observation", "id": "07577874", "effectiveDateTime": "2029-04-03T10:00:00Z",
                      "code": {"text": "Temperature Oral"}, "valueQuantity": {"value": 11.1, "unit": "degC"}},
                     {"resourceType": "Observation", "id": "09014210", "effectiveDateTime": "2026-10-14T10:00:00Z",
                      "code": {"text": "Pulse Sitting"}, "valueQuantity": {"value": 120, "unit": "bpm"}}],
    "/Encounter": [{"resourceType": "Encounter", "id": "97958996", "class": {"display": "Outpatient"}}],
}


class ChartServer(FakeFhir):
    def get(self, url, params=None, headers=None, timeout=None):
        path = url.removeprefix(self.base)
        if path in CHART:
            self.gets.append((path, dict(params or {}), dict(headers or {})))
            return Response(200, bundle(CHART[path]))
        return super().get(url, params, headers, timeout)


@pytest.fixture
def oracle_brief(monkeypatch):
    import mcp_servers.chart_brief.server as cb
    import mcp_servers.medication_review.server as mr
    monkeypatch.setattr(mr, "fetch_missing", lambda cfg, meds, lookup=None: [])
    known = ("acetaminophen", "diphenhydramine", "captopril")
    monkeypatch.setattr(cb, "_ingredient_reader",
                        lambda profile: lambda m: [i for i in known if i in (m.get("name") or "").lower()])
    m = ORACLE.fhir_map
    c = FhirClient(m.server["base_url"], None, ChartServer(m.server["base_url"]))
    return cb.brief(None, ORACLE, cb.load_config(ORACLE), "Give me a brief on Ines Testarossa, born 1971-03-04",
                    list(ORACLE.data_classes), c)["answer"]


def test_repeated_orders_are_shown_once_with_their_count_and_dates(oracle_brief):
    lines = [x for x in oracle_brief.splitlines() if x.startswith("- Captopril, ordered")]
    assert lines == ["- Captopril, ordered 2024-11-11 (3 entries in the record, 2024-11-09 to 2024-11-11) [med-41000003]"]
    assert "Captopril appears in 3 current orders [med-41000001, med-41000002, med-41000003]." in oracle_brief
    assert oracle_brief.count("No reason recorded for Captopril") == 1
    assert "No reason recorded for Captopril in any of its 3 orders" in oracle_brief


def test_an_ingredient_in_two_medications_is_reported(oracle_brief):
    assert ("Acetaminophen is an ingredient of 2 current medications: Acetaminophen (Tylenol 325 mg oral capsule) "
            "[med-06635049]; Acetaminophen-diphenhydramine") in oracle_brief


def test_allergy_names_are_not_doubled(oracle_brief):
    assert "- Allergy to collodion [alg-12774995]" in oracle_brief
    assert "- Allergy to Isosorbide dinitrate [alg-12781535]" in oracle_brief
    assert "Allergy to Allergy" not in oracle_brief


def test_venous_hypertension_is_not_checked_against_blood_pressure_medications(oracle_brief):
    assert "none of the medications the office lists for hypertension" not in oracle_brief
    # and it is not the reason for captopril either: captopril's reason is still missing
    assert "No reason recorded for Captopril in any of its 3 orders [med-41000001], and the problem list has none" \
        in oracle_brief


def test_impossible_and_future_vitals_are_flagged_and_other_words_for_a_vital_match(oracle_brief):
    assert "Temperature Oral 11.1 degC, measured 2029-04-03 [vit-07577874] — dated in the future (2029-04-03); " \
           "outside the range a measurement could be (30–45); check the record" in oracle_brief
    assert "Pulse Sitting 120 bpm, measured 2026-10-14 [vit-09014210] — above the office threshold (100)" in oracle_brief


def test_a_missing_visit_date_is_said_plainly(oracle_brief):
    assert "- Visit, date not recorded [enc-97958996]" in oracle_brief
    assert "unknown" not in oracle_brief


def test_a_value_in_other_units_is_not_compared():
    from mcp_servers.chart_brief.server import check_vitals, load_config
    cfg = load_config(ORACLE)
    lines, _ = check_vitals([{"kind": "Temperature Oral", "value": 98.6, "units": "[degF]", "date": "2026-01-01",
                              "_id": "vit-1"}], cfg, today="2026-11-12")
    assert lines == []


# --- a list read only in part says so ----------------------------------------------------------------------

def test_search_reads_pages_to_the_limit_and_says_when_the_record_has_more():
    class Pages(FakeFhir):
        def get(self, url, params=None, headers=None, timeout=None):
            self.gets.append((url, dict(params or {}), {}))
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("page", ["1"])[0])
            b = bundle([{"resourceType": "AllergyIntolerance", "id": f"a{page}-{i}"} for i in range(50)])
            if page < 8:
                b["link"] = [{"relation": "next", "url": f"{self.base}/AllergyIntolerance?page={page + 1}"}]
            return Response(200, b)
    base = "https://fhir.example.test/r4"
    c = FhirClient(base, None, Pages(base))
    found, more = c.search_all("AllergyIntolerance", {"patient": "1", "_count": 200}, 200)
    assert len(found) == 200 and more
    assert c.session.gets[0][1]["_count"] == 50                 # asked in pages of 50
    found, more = c.search_all("AllergyIntolerance", {"patient": "1", "_count": 20}, 20)
    assert len(found) == 20 and more


def test_a_whole_list_is_not_called_incomplete():
    base = "https://fhir.example.test/r4"
    found, more = FhirClient(base, None, FakeFhir(base)).search_all("MedicationRequest", {"patient": "9"}, 200)
    assert len(found) == 1 and not more


def test_a_cut_off_allergy_list_is_said_so_in_place_and_as_the_first_check(monkeypatch):
    import mcp_servers.chart_brief.server as cb
    import mcp_servers.medication_review.server as mr
    monkeypatch.setattr(mr, "fetch_missing", lambda cfg, meds, lookup=None: [])

    class More(ChartServer):
        def get(self, url, params=None, headers=None, timeout=None):
            r = super().get(url, params, headers, timeout)
            if url.endswith("/AllergyIntolerance"):
                r._body["link"] = [{"relation": "next", "url": url + "?page=2"}]
            return r
    m = ORACLE.fhir_map
    c = FhirClient(m.server["base_url"], None, More(m.server["base_url"]))
    c.max_pages = 1
    answer = cb.brief(None, ORACLE, cb.load_config(ORACLE), "Give me a brief on Ines Testarossa, born 1971-03-04",
                      list(ORACLE.data_classes), c)["answer"]
    assert "- 2 shown; the record has more. Check it directly." in answer
    first_check = answer.split("Checks (what the record does or does not show):\n")[1].splitlines()[0]
    assert first_check.startswith("- Allergies is incomplete: 2 entries were read and the record has more")
    assert "Recent visits is incomplete" not in answer           # the latest only: not a whole list


def test_check_lines_name_medications_as_the_list_does(oracle_brief):
    assert "No reason recorded for Acetaminophen (Tylenol" in oracle_brief


# --- production hardening: order, waiting, signing in again, record numbers ---------------------------------

class Scripted:
    """Answers GETs from a list of (status, body, headers)."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(headers or {}))
        status, body, h = self.answers.pop(0)
        r = Response(status, body)
        r.headers = h
        return r


def test_a_busy_server_is_waited_for_as_it_asks_then_read():
    waits = []
    s = Scripted([(429, {}, {"Retry-After": "3"}), (503, {}, {}),
                  (200, {"resourceType": "CapabilityStatement"}, {})])
    c = FhirClient("https://fhir.example.test/r4", None, s, sleep=waits.append, jitter=None)
    assert c.capability()["resourceType"] == "CapabilityStatement"
    assert waits == [3.0, 2.0]                        # its Retry-After, then 2 s


def test_a_wait_the_server_did_not_set_is_jittered_and_its_own_is_not():
    waits = []
    s = Scripted([(503, {}, {}), (429, {}, {"Retry-After": "3"}), (200, {"resourceType": "CapabilityStatement"}, {})])
    FhirClient("https://fhir.example.test/r4", None, s, sleep=waits.append).capability()
    assert 0.5 <= waits[0] <= 1.0 and waits[1] == 3.0   # 1 s backoff, half of it random; Retry-After exactly


def test_a_long_retry_after_is_not_waited_for_and_three_busy_answers_fail():
    waits = []
    s = Scripted([(429, {}, {"Retry-After": "600"})] * 3)
    c = FhirClient("https://fhir.example.test/r4", None, s, sleep=waits.append)
    with pytest.raises(FhirError, match="429"):
        c.capability()
    assert waits == [10.0, 10.0]


def test_a_refused_token_is_replaced_once():
    class Auth:
        n = 0

        def token(self):
            return f"t-{self.n}"

        def invalidate(self):
            self.n += 1
    s = Scripted([(401, {}, {}), (200, {"resourceType": "CapabilityStatement"}, {})])
    FhirClient("https://fhir.example.test/r4", Auth(), s).capability()
    assert [h["Authorization"] for h in s.calls] == ["Bearer t-0", "Bearer t-1"]
    s2 = Scripted([(401, {}, {}), (401, {}, {})])
    with pytest.raises(FhirError, match="401"):
        FhirClient("https://fhir.example.test/r4", Auth(), s2).capability()


def test_parallel_reads_sign_in_once():
    import concurrent.futures

    class SlowToken(FakeFhir):
        def post(self, url, data=None, headers=None, timeout=None):
            time.sleep(0.05)
            return super().post(url, data, headers, timeout)
    fake = SlowToken("")
    auth = SmartBackendAuth(SmartBackendSettings(TOKEN_URL, "a", b"", None, None, "s"), fake)
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        assert set(pool.map(lambda _: auth.token(), range(6))) == {"t-1"}
    assert len(fake.posts) == 1


def test_vitals_and_visits_are_the_newest_by_date_whatever_the_servers_order():
    m = ORACLE.fhir_map
    visits, vitals = m.record("visits"), m.record("vitals")
    found = [{"id": str(i), "period": {"start": d}} for i, d in enumerate(
        ["2024-01-05", "2026-09-01", "2025-03-02", "2026-11-01", "2023-07-07"])]
    assert [v["period"]["start"] for v in visits.arrange(found)] == ["2026-11-01", "2026-09-01", "2025-03-02"]
    from datetime import date
    p = vitals.params("12724065", today=date(2026, 11, 12))
    assert p["date"] == "ge2024-11-12T00:00:00Z" and p["_count"] == 250 and p["category"] == "vital-signs"
    readings = [{"effectiveDateTime": "2025-01-01T08:00:00Z"}, {"issued": "2026-10-30T09:00:00Z"},
                {"effectivePeriod": {"start": "2026-02-01"}}]
    assert vitals.arrange(readings)[0] == {"issued": "2026-10-30T09:00:00Z"}


def test_a_cut_off_vitals_read_says_the_latest_may_be_missing(monkeypatch):
    import mcp_servers.chart_brief.server as cb
    import mcp_servers.medication_review.server as mr
    monkeypatch.setattr(mr, "fetch_missing", lambda cfg, meds, lookup=None: [])

    class More(ChartServer):
        def get(self, url, params=None, headers=None, timeout=None):
            r = super().get(url, params, headers, timeout)
            if url.endswith("/Observation"):
                r._body["link"] = [{"relation": "next", "url": url + "?page=2"}]
            return r
    m = ORACLE.fhir_map
    c = FhirClient(m.server["base_url"], None, More(m.server["base_url"]))
    c.max_pages = 1
    answer = cb.brief(None, ORACLE, cb.load_config(ORACLE), "Give me a brief on Ines Testarossa, born 1971-03-04",
                      list(ORACLE.data_classes), c)["answer"]
    assert "- 2 read; the record has more, so the latest may be missing. Check it directly." in answer
    assert "- Latest vitals may not be the latest: 2 entries were read" in answer


def test_a_record_number_finds_the_patient_and_the_birth_date_must_match():
    from dataclasses import replace
    m = replace(ORACLE.fhir_map, identifier={"param": "identifier", "system": "urn:oid:2.16.840.1.113883.6.1000"})

    class ById(FakeFhir):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/Patient") and "identifier" in (params or {}):
                self.gets.append(("/Patient", dict(params), {}))
                ok = params["identifier"] == "urn:oid:2.16.840.1.113883.6.1000|MRN-7781"
                return Response(200, bundle([PATIENT] if ok else []))
            return super().get(url, params, headers, timeout)
    fake = ById(m.server["base_url"])
    c = FhirClient(m.server["base_url"], None, fake)
    p, why = find_patient(c, m, "Brief on MRN: MRN-7781, born 1971-03-04")
    assert p["id"] == "90000001" and fake.gets[0][1] == {"identifier": "urn:oid:2.16.840.1.113883.6.1000|MRN-7781"}
    assert find_patient(c, m, "medical record number MRN-7781, born 1980-01-01") == (None, "none")
    assert find_patient(c, ORACLE.fhir_map, "MRN 7781 born 1971-03-04")[1] == "none"   # not configured: by name only
