"""The FHIR records source (ADR 013): SMART Backend sign-in, a GET-only client, the mapping, the
records server with role filtering, and the medication review reading Epic-style MedicationRequests.

No network: a fake FHIR server answers from invented resources in the shape Epic returns.
Invented test data only: not Epic's, not real people.
"""

import json
import shutil
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fhir.client import FhirClient, FhirError, SmartBackendAuth, SmartBackendSettings, client_from_settings
from mcp_servers.fhir_records.server import name_candidates, search
from mcp_servers.medication_review.server import load_config, review
from profiles.fhir_map import FhirMappingError, get_path, human_name, parse, render, Text
from profiles.loader import ProfileError, load_profile, load_profile_from

BASE = "https://fhir.example.test/api/FHIR/R4"
TOKEN_URL = "https://fhir.example.test/oauth2/token"
EPIC = load_profile("clinic-epic")
CLINIC = load_profile("clinic-assistant")

PATIENT = {"resourceType": "Patient", "id": "eTestPatient0001AbCd", "gender": "female", "birthDate": "1987-09-12",
           "name": [{"use": "official", "family": "Testwell", "given": ["Nora"]}],
           "address": [{"city": "Madison"}]}
OTHER = {"resourceType": "Patient", "id": "eTestPatient0002XyZw", "name": [{"family": "Testwell", "given": ["Nora"]}]}
MEDS = [
    {"resourceType": "MedicationRequest", "id": "eMedReq.warf-001", "status": "active", "authoredOn": "2026-02-01",
     "medicationCodeableConcept": {"text": "warfarin 5 MG tablet",
                                   "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "855332"}]},
     "reasonCode": [{"text": "Atrial fibrillation"}]},
    {"resourceType": "MedicationRequest", "id": "eMedReq.ibu-0002", "status": "active", "authoredOn": "2026-09-28",
     "medicationReference": {"reference": "Medication/eMedIbu", "display": "ibuprofen 400 MG tablet"}},
]
MEDICATION = {"resourceType": "Medication", "id": "eMedIbu",
              "code": {"text": "ibuprofen 400 MG tablet",
                       "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "197806"}]}}
ENCOUNTERS = [{"resourceType": "Encounter", "id": "eEnc000000000001", "class": {"display": "Office Visit"},
               "type": [{"text": "Follow-up"}], "period": {"start": "2026-09-30T14:00:00Z"},
               "reasonCode": [{"text": "Knee pain"}]}]


def bundle(resources, next_url=None):
    b = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


class Response:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeServer:
    """Answers GETs from the invented resources and token POSTs; records every request."""

    def __init__(self, patients=(PATIENT,)):
        self.patients, self.gets, self.posts = list(patients), [], []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url.removeprefix(BASE), dict(params or {}), headers))
        path = url.removeprefix(BASE)
        if path == "/metadata":
            return Response(200, {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1",
                                  "rest": [{"resource": [{"type": t} for t in ("Patient", "MedicationRequest",
                                                                               "Medication", "Encounter")]}]})
        if path == "/Patient":
            if "birthdate" not in params:        # like Epic's sandbox: name alone finds no one
                return Response(200, bundle([]))
            return Response(200, bundle([p for p in self.patients
                                         if p["name"][0]["family"] == params.get("family")
                                         and params.get("given") in p["name"][0]["given"]
                                         and p.get("birthDate", "1987-09-12") == params["birthdate"]]))
        if path == "/MedicationRequest":
            return Response(200, bundle(MEDS + [{"resourceType": "OperationOutcome", "issue": []}]))
        if path == "/Medication/eMedIbu":
            return Response(200, MEDICATION)
        if path == "/Encounter":
            return Response(200, bundle(ENCOUNTERS))
        return Response(404, {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "not found"}]})

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data))
        return Response(200, {"access_token": "test-token", "token_type": "Bearer", "expires_in": 300,
                              "scope": "system/Patient.read"})


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    return pem, key.public_key()


def mapping(base=BASE):
    m = EPIC.fhir_map
    return parse({**_raw_epic(), "server": {"base_url": base}}, "test", set(EPIC.data_classes)) if base else m


def _raw_epic():
    import yaml
    return yaml.safe_load((EPIC.path / "fhir.yaml").read_text())


# --- sign-in ---------------------------------------------------------------------------------------

def test_the_assertion_is_an_rs384_jwt_signed_by_the_private_key(keypair):
    pem, public = keypair
    auth = SmartBackendAuth(SmartBackendSettings(TOKEN_URL, "client-123", pem, "gaai-sandbox-1"), FakeServer(),
                            clock=lambda: 1_800_000_000)
    token = auth.assertion()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS384" and header["kid"] == "gaai-sandbox-1"
    claims = jwt.decode(token, public, algorithms=["RS384"], audience=TOKEN_URL,
                        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False})
    assert claims["iss"] == claims["sub"] == "client-123" and claims["exp"] - claims["iat"] <= 300
    assert auth.assertion() != token or True                       # a new jti each time
    assert jwt.decode(auth.assertion(), options={"verify_signature": False})["jti"] != claims["jti"]


def test_the_token_is_requested_once_and_reused_until_it_nearly_expires(keypair):
    server, now = FakeServer(), [1_800_000_000]
    auth = SmartBackendAuth(SmartBackendSettings(TOKEN_URL, "c", keypair[0], "k"), server, clock=lambda: now[0])
    assert auth.token() == auth.token() == "test-token" and len(server.posts) == 1
    form = server.posts[0][1]
    assert form["grant_type"] == "client_credentials" and form["client_assertion_type"].endswith("jwt-bearer")
    now[0] += 290
    auth.token()
    assert len(server.posts) == 2


def test_secrets_come_from_the_environment_and_must_exist(tmp_path, keypair):
    server = {"base_url": BASE, "auth": "smart_backend", "token_url": TOKEN_URL, "client_id_env": "X_ID",
              "private_key_env": "X_KEY", "key_id": "k"}
    with pytest.raises(FhirError, match="X_ID"):
        client_from_settings(server, environ={})
    with pytest.raises(FhirError, match="not found"):
        client_from_settings(server, environ={"X_ID": "c", "X_KEY": str(tmp_path / "missing.pem")})
    (tmp_path / "k.pem").write_bytes(keypair[0])
    c = client_from_settings(server, environ={"X_ID": "c", "X_KEY": str(tmp_path / "k.pem")}, session=FakeServer())
    assert c.auth.s.client_id == "c"


def test_the_mapping_refuses_secrets_written_in_the_file():
    raw = _raw_epic()
    raw["server"]["client_id"] = "9a29f08a"
    with pytest.raises(FhirMappingError, match=r"\.env"):
        parse(raw, "fhir.yaml", set(EPIC.data_classes))


# --- the client --------------------------------------------------------------------------------------

def test_the_client_only_ever_sends_get_to_the_fhir_server(keypair):
    server = FakeServer()
    auth = SmartBackendAuth(SmartBackendSettings(TOKEN_URL, "c", keypair[0], "k"), server)
    c = FhirClient(BASE, auth, server)
    c.capability()
    c.search("Patient", {"given": "Nora", "family": "Testwell"})
    assert all(not hasattr(FhirClient, verb) for verb in ("create", "update", "delete", "post", "put", "patch"))
    assert all(h["Authorization"] == "Bearer test-token" for _, _, h in server.gets)
    assert [u for u, _ in server.posts] == [TOKEN_URL]                # the only POST is the token request


def test_search_follows_next_links_but_never_off_the_server():
    class Paged(FakeServer):
        def get(self, url, params=None, headers=None, timeout=None):
            self.gets.append((url, params, headers))
            if url.endswith("page2"):
                return Response(200, bundle([{"resourceType": "Encounter", "id": "b"}]))
            return Response(200, bundle([{"resourceType": "Encounter", "id": "a"}], f"{BASE}/Encounter?page2"))
    assert [r["id"] for r in FhirClient(BASE, session=Paged()).search("Encounter", {})] == ["a", "b"]

    class Elsewhere(FakeServer):
        def get(self, url, params=None, headers=None, timeout=None):
            return Response(200, bundle([], "https://evil.example/steal"))
    with pytest.raises(FhirError, match="outside"):
        FhirClient(BASE, session=Elsewhere()).search("Encounter", {})


def test_errors_say_what_the_server_said():
    class Refusing(FakeServer):
        def get(self, url, params=None, headers=None, timeout=None):
            return Response(403, {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "scope missing"}]})
    with pytest.raises(FhirError, match="403.*scope missing"):
        FhirClient(BASE, session=Refusing()).search("Patient", {})


# --- the mapping -----------------------------------------------------------------------------------

def test_paths_names_and_templates():
    assert get_path(PATIENT, "address.city") == "Madison"
    assert get_path(PATIENT, "name[0].given") == "Nora" and get_path(PATIENT, "name[3].family") is None
    assert human_name(PATIENT) == "Nora Testwell"
    t = Text("{subject}: {medicationCodeableConcept.text,medicationReference.display}, ordered {authoredOn|date}.",
             (("reasonCode.text", " Reason recorded: {reasonCode.text}."),))
    assert render(t, MEDS[0], {"subject": "Nora Testwell"}) == \
        "Nora Testwell: warfarin 5 MG tablet, ordered 2026-02-01. Reason recorded: Atrial fibrillation."
    assert render(t, MEDS[1], {"subject": "N"}) == "N: ibuprofen 400 MG tablet, ordered 2026-09-28."


def test_mapping_checks():
    raw = _raw_epic()
    raw["records"]["medications"]["text"] = "{subject}: {medication; DROP}"
    with pytest.raises(FhirMappingError, match="not a simple FHIR path"):
        parse(raw, "fhir.yaml", set(EPIC.data_classes))
    raw = _raw_epic()
    raw["records"]["medications"]["data_class"] = "claims"
    with pytest.raises(FhirMappingError, match="not in roles.yaml"):
        parse(raw, "fhir.yaml", set(EPIC.data_classes))


def test_dates_of_birth_are_read_in_the_usual_forms():
    from mcp_servers.fhir_records.server import birthdates_in
    for q in ("Camila Lopez born 1987-09-12", "Camila Lopez, DOB 9/12/1987", "Camila Lopez, born September 12, 1987",
              "Camila Lopez (12 Sep 1987)"):
        assert birthdates_in(q) == ["1987-09-12"], q
    assert birthdates_in("Camila Lopez") == [] and birthdates_in("on 13/45/2020") == []


def test_without_a_date_of_birth_the_assistant_asks_for_one():
    server = FakeServer()
    out = search(FhirClient(BASE, session=server), mapping(), "What medications is Nora Testwell taking?",
                 list(EPIC.data_classes))
    assert out["results"] == [] and "date of birth" in out["note"]
    assert server.gets == []                                          # nothing was even asked of Epic


def test_names_are_read_from_the_question():
    assert name_candidates("Check Nora Testwell's medications for interactions.") == [("Nora", "Testwell")]
    assert name_candidates("What medications is Camila Lopez taking?") == [("Camila", "Lopez")]
    assert name_candidates("When was Mary Ann Smith seen?") == [("Mary", "Smith")]
    assert name_candidates("what is our cancellation policy") == []
    assert name_candidates("Camila Lopez, born September 12, 1987") == [("Camila", "Lopez")]


# --- the records server --------------------------------------------------------------------------

def test_records_have_the_same_shape_and_source_ids_as_the_database_source():
    server = FakeServer()
    out = search(FhirClient(BASE, session=server), mapping(), "What medications is Nora Testwell (born 1987-09-12) taking?",
                 list(EPIC.data_classes))["results"]
    ids = [r["id"] for r in out]
    assert ids[0] == "pt-0001AbCd" and "med-qwarf001" in ids
    assert all(EPIC.source_id_pattern.fullmatch(i) for i in ids)
    pat = out[0]
    assert pat["text"] == "Nora Testwell, born 1987-09-12, sex female, lives in Madison." and pat["status"]
    assert any("Follow-up visit on 2026-09-30. Reason recorded: Knee pain." in r["text"] for r in out)


def test_a_role_never_causes_a_request_for_data_it_may_not_see():
    server = FakeServer()
    front = list(EPIC.roles["front_desk"].data_classes)
    out = search(FhirClient(BASE, session=server), mapping(), "Nora Testwell born 9/12/1987", front)["results"]
    asked = {u for u, _, _ in server.gets}
    assert "/MedicationRequest" not in asked and "/Encounter" in asked
    assert all(set(r["data_classes"]) <= set(front) for r in out)
    assert not any("Knee pain" in r["text"] for r in out)            # visit reasons are visit_clinical


def test_two_patients_with_the_same_name_return_nothing():
    server = FakeServer(patients=(PATIENT, OTHER))
    out = search(FhirClient(BASE, session=server), mapping(), "Nora Testwell, born September 12, 1987", list(EPIC.data_classes))
    assert out["results"] == [] and "more than one" in out["note"]


def test_a_server_failure_returns_nothing_and_says_why():
    class Down(FakeServer):
        def get(self, url, params=None, headers=None, timeout=None):
            return Response(503, {"error": "unavailable"})
    out = search(FhirClient(BASE, session=Down()), mapping(), "Nora Testwell 1987-09-12", ["patient_details"])
    assert out["results"] == [] and "did not answer" in out["note"]


# --- medication review from Epic-style records -----------------------------------------------------

def test_medication_review_reads_medication_requests_and_uses_rxnorm_codes(tmp_path):
    dst = tmp_path / "clinic-epic"
    shutil.copytree(EPIC.path, dst)
    labels = {"downloaded": "2026-10-20", "rxcui": {"855332": ["warfarin"], "197806": ["ibuprofen"]},
              "ingredients": {
                  "warfarin": {"set_id": "aaaabbbb-0000", "title": "Warfarin", "classes": ["Vitamin K Antagonist"],
                               "sections": {"drug_interactions": ["Test text: NSAIDs such as ibuprofen raise bleeding risk."]}},
                  "ibuprofen": {"set_id": "ccccdddd-0000", "title": "Ibuprofen", "classes": [],
                                "sections": {"drug_interactions": ["Test text."]}}}}
    (dst / "data" / "drug_labels.json").write_text(json.dumps(labels))
    p = load_profile_from(dst)
    p = p.__class__(**{**p.__dict__, "fhir_map": parse({**_raw_epic(), "server": {"base_url": BASE}}, "t",
                                                       set(p.data_classes))})
    server = FakeServer()
    out = review(None, p, load_config(p), "Check Nora Testwell's medications for interactions, born 1987-09-12",
                 ["medications"], FhirClient(BASE, session=server))
    a = out["answer"]
    assert "warfarin 5 MG tablet [med-qwarf001]" in a and "ibuprofen 400 MG tablet" in a
    assert 'mentions "ibuprofen"' in a and "lbl-aaaabbbb" in a and "not a recommendation" in a
    assert "/Medication/eMedIbu" in {u for u, _, _ in server.gets}     # the coded drug behind a reference
    cited = {m.group(0) for m in p.source_id_pattern.finditer(a)}
    assert cited <= {r["id"] for r in out["results"]}


# --- the profile -----------------------------------------------------------------------------------

def test_the_epic_profile_is_the_clinic_with_another_records_source():
    for f in ("rules.yaml", "responses.yaml", "prompt.md", "classifier_policy.md"):
        assert (EPIC.path / f).read_text() == (CLINIC.path / f).read_text(), f"{f} drifted from clinic-assistant"
    assert EPIC.classifier.guard_policy == CLINIC.classifier.guard_policy
    assert EPIC.records_map is None and EPIC.fhir_map is not None and not EPIC.actions
    assert EPIC.roles["clinician"].tools == ("search_knowledge", "search_records", "medication_review",
                                             "medication_list")
    assert "medication_review" not in EPIC.roles["nurse"].tools and "medication_list" in EPIC.roles["nurse"].tools
    assert "medication_list" not in EPIC.roles["front_desk"].tools
    assert EPIC.tools["search_records"].server.endswith(":8104/mcp")
    assert "client_id" not in (EPIC.path / "fhir.yaml").read_text().replace("client_id_env", "")


def test_a_profile_cannot_have_two_records_sources(tmp_path):
    dst = tmp_path / "clinic-epic"
    shutil.copytree(EPIC.path, dst)
    text = (dst / "profile.yaml").read_text().replace("fhir: fhir.yaml", "fhir: fhir.yaml\nrecords_map: fhir.yaml")
    (dst / "profile.yaml").write_text(text)
    with pytest.raises(ProfileError):
        load_profile_from(dst)


def test_the_published_key_is_public_only():
    jwks = json.loads((Path(__file__).resolve().parent.parent / "docs" / "epic" / "jwks.json").read_text())
    key = jwks["keys"][0]
    assert key["kid"] == EPIC.fhir_map.server["key_id"] and key["alg"] == "RS384"
    assert not {"d", "p", "q", "dp", "dq", "qi"} & set(key)


def test_medication_review_asks_for_the_date_of_birth(tmp_path):
    dst = tmp_path / "clinic-epic"
    shutil.copytree(EPIC.path, dst)
    (dst / "data" / "drug_labels.json").write_text(json.dumps({"ingredients": {"x": {"set_id": "1"}}}))
    p = load_profile_from(dst)
    p = p.__class__(**{**p.__dict__, "fhir_map": mapping()})
    out = review(None, p, load_config(p), "Check Nora Testwell's medications for interactions", ["medications"],
                 FhirClient(BASE, session=FakeServer()))
    assert "date of birth" in out["answer"] and out["results"] == []


def test_a_missing_permission_is_named():
    class NoScope(FakeServer):
        def get(self, url, params=None, headers=None, timeout=None):
            r = Response(403, {})
            r.text = ""
            r.headers = {"WWW-Authenticate": 'Bearer error="insufficient_scope", error_description="not authorized"'}
            return r
    with pytest.raises(FhirError, match="insufficient_scope.*APIs"):
        FhirClient(BASE, session=NoScope()).search("MedicationRequest", {"patient": "x"})



def test_a_medication_list_is_the_record_s_own_lines_with_no_model(tmp_path):
    from agents.tool_choice import tool_denied_reply, tools_to_call
    from mcp_servers.medication_review.server import medication_list
    p = EPIC.__class__(**{**EPIC.__dict__, "fhir_map": mapping()})
    q = "What medications is Nora Testwell (born 1987-09-12) taking?"
    assert tools_to_call(p, "planner", q)[0] == ["medication_list"]
    server = FakeServer()
    out = medication_list(None, p, load_config(p), q, ["medications"], FhirClient(BASE, session=server))
    a = out["answer"]
    assert "warfarin 5 MG tablet, ordered 2026-02-01. Reason recorded: Atrial fibrillation. [med-qwarf001]" in a
    assert "not a recommendation" in a and all(r["data_classes"] == ["medications"] for r in out["results"])
    assert {u for u, _, _ in server.gets} == {"/Patient", "/MedicationRequest"}     # nothing else is fetched
    cited = {m.group(0) for m in p.source_id_pattern.finditer(a)}
    assert cited <= {r["id"] for r in out["results"]}
    assert "not available to your role (Front desk and billing)" in tool_denied_reply(p, "medication_list", "front_desk")
    assert "date of birth" in medication_list(None, p, load_config(p), "What medications is Nora Testwell taking?",
                                              ["medications"], FhirClient(BASE, session=FakeServer()))["answer"]
