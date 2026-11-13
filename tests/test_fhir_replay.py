"""Epic and Oracle Health, replayed from recordings (ADR 036).

Each folder in tests/fixtures/fhir/ holds a vendor sandbox's answers in the recording format
(fhir/recording.py). The client, the mapping and the records search run unchanged against them; a
request the recording does not have fails the test (UnrecordedRequest), so a change in what the
assistant asks for is caught here.

The folders start hand-built from what the sandboxes sent on 2026-11-12, with invented patients;
`scripts/fhir_record.py` replaces them with real recordings of the vendors' synthetic test patients.
The tests below assert only what holds for either: the envelope, the paging, and the quirks.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from fhir.capabilities import Capabilities, check_map
from fhir.client import FhirClient, FhirError
from fhir.metrics import Metrics
from fhir.recording import ReplaySession, UnrecordedRequest
from fhir.transport import ServerPolicy, Transport
from mcp_servers.fhir_records.server import search
from profiles.loader import load_profile

FIXTURES = Path(__file__).parent / "fixtures" / "fhir"
VENDORS = sorted(p.name for p in FIXTURES.iterdir() if (p / "index.json").exists())


def replay(vendor: str):
    session = ReplaySession(FIXTURES / vendor)
    profile = load_profile(session.meta["profile"])
    m = profile.fhir_map
    metrics = Metrics()
    transport = Transport(m.server["base_url"], ServerPolicy.from_settings(m.server), session=session, metrics=metrics)
    return session, m, FhirClient(m.server["base_url"], None, transport=transport), metrics


def test_there_is_a_recording_for_epic_and_for_oracle_health():
    assert {"epic", "oracle"} <= set(VENDORS)


@pytest.mark.parametrize("vendor", VENDORS)
def test_a_recording_holds_no_secret(vendor):
    text = "".join(p.read_text() for p in (FIXTURES / vendor).rglob("*.json"))
    for marker in ("Authorization", "Bearer ", "access_token", "refresh_token", "Set-Cookie", "client_secret"):
        assert marker not in text


@pytest.mark.parametrize("vendor", VENDORS)
def test_the_server_offers_everything_the_profile_reads(vendor):
    _, m, client, _ = replay(vendor)
    findings = check_map(Capabilities.parse(client.capability()), m)
    assert [f for f in findings if f.level == "error"] == []


@pytest.mark.parametrize("vendor", VENDORS)
def test_every_record_type_reads_and_validates(vendor):
    session, m, client, metrics = replay(vendor)
    pid = session.meta["patient"]["id"]
    assert client.read(m.resource, pid)["id"] == pid
    today = dt.date.fromisoformat(session.meta["recorded_on"])
    for r in m.records:
        found, more = client.search_all(r.resource, r.params(pid, today), r.limit)
        assert all(x["resourceType"] == r.resource for x in found)
        assert more is False, r.name                    # every recording ends with its last page
    assert metrics.total("fhir_response_invalid_total") == 0


@pytest.mark.parametrize("vendor", VENDORS)
def test_the_records_search_finds_the_patient_by_name_and_birth_date(vendor):
    session, m, client, _ = replay(vendor)
    out = search(client, m, session.meta["patient"]["query"], sorted(m.data_classes()))
    assert "note" not in out, out                      # found, and every record type read
    assert {r["data_classes"][0] for r in out["results"]} >= {m.subject_class, "medications", "problems"}


@pytest.mark.parametrize("vendor", VENDORS)
def test_a_request_the_recording_does_not_have_fails_the_test(vendor):
    _, _, client, _ = replay(vendor)
    with pytest.raises(UnrecordedRequest):
        client.search("Procedure", {"patient": "nobody"})


# --- the vendors' quirks ------------------------------------------------------------------------

def test_oracle_health_pages_with_its_own_page_context_and_both_pages_are_read():
    session, m, client, _ = replay("oracle")
    vitals = m.record("vitals")
    found, more = client.search_all(vitals.resource, vitals.params(session.meta["patient"]["id"]), vitals.limit)
    assert len(found) == 3 and not more
    assert any(("-pageContext", "pc_Q2F0YWxvZw") in map(tuple, q) for _, q in session.requests)


def test_oracle_health_refuses_a_date_without_a_time_and_the_message_says_why():
    session, _, client, _ = replay("oracle")
    with pytest.raises(FhirError, match="must have a time"):
        client.search("Encounter", {"patient": session.meta["patient"]["id"], "_count": 50, "date": "ge2023-11-13"})


def test_oracle_health_visits_come_back_out_of_order_and_the_mapping_puts_the_newest_first():
    session, m, client, _ = replay("oracle")
    visits = m.record("visits")
    found = visits.arrange(client.search(visits.resource, visits.params(session.meta["patient"]["id"]), visits.limit))
    assert [v["period"]["start"][:10] for v in found] == ["2026-10-02", "2026-05-15", "2025-01-20"]


def test_epic_answers_an_empty_search_with_a_warning_which_is_not_a_cut_off_list():
    session, m, client, _ = replay("epic")
    visits = m.record("visits")
    found, more = client.search_all(visits.resource, visits.params(session.meta["patient"]["id"]), visits.limit)
    assert found == [] and more is False


def test_a_replayed_answer_uses_the_base_it_is_replayed_at():
    session = ReplaySession(FIXTURES / "oracle", "https://fhir.example.test/r4/tenant")
    body = session.get("https://fhir.example.test/r4/tenant/Patient",
                       params={"given": "Ines", "family": "Testarossa", "birthdate": "1971-03-04"}).json()
    assert json.dumps(body).count("https://fhir.example.test/r4/tenant/Patient/") == 1
