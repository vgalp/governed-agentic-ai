"""The county benefits profile: a second, unrelated use case built from configuration
only. It runs on the same code as the clinic: same records server, gateway, policies,
guardrails and pipeline. All data is fictional (profiles/county-benefits/data)."""

import json
import sqlite3

import pytest

from guardrails.rules import check_input, check_output
from profiles.loader import load_profile

server = pytest.importorskip("mcp_servers.records.server")

P = load_profile("county-benefits")
MAP = P.records_map
SEED = P.path / "data" / "seed.sql"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "county.db"
    conn = sqlite3.connect(path)
    conn.executescript(SEED.read_text())
    conn.commit()
    conn.close()
    return path


def lookup(db, role, query="Tell me about Rosa Delgado"):
    return server.search(db, query, list(P.roles[role].data_classes), MAP)["results"]


def kinds(results):
    return {r["id"].split("-")[0] for r in results}


# --- the profile is configuration only ------------------------------------------------

def test_profile_has_no_code():
    assert not list(P.path.rglob("*.py"))


def test_profile_loads_with_its_own_roles_and_tools():
    assert set(P.roles) == {"intake_worker", "eligibility_worker", "supervisor", "auditor"}
    assert set(P.tools) == {"search_knowledge", "search_records"}
    assert P.actions == {}                          # read-only: no changes in this profile
    assert P.tools["search_records"].data_label in P.routing["never_external_labels"]


def test_mapping_matches_the_seed_database(db):
    with sqlite3.connect(db) as conn:
        assert server.check_schema(conn, MAP) == []


# --- what each role gets ------------------------------------------------------------

def test_intake_worker_never_gets_income_or_payments(db):
    out = lookup(db, "intake_worker")
    assert kinds(out) == {"apl", "case", "doc"}


def test_eligibility_worker_sees_income_and_payments(db):
    assert {"inc", "pay"} <= kinds(lookup(db, "eligibility_worker"))


def test_auditor_gets_no_case_records(db):
    assert lookup(db, "auditor") == []
    assert "search_records" not in P.roles["auditor"].tools


def test_records_read_as_written_in_records_yaml(db):
    texts = {r["id"]: r["text"] for r in lookup(db, "supervisor", "Tell me about Dana Whitfield")}
    assert texts["doc-3001"] == ("Dana Whitfield: Proof of income (pay stubs, last 30 days), missing. "
                                 "Needed by 2026-10-21.")
    assert texts["inc-4001"] == "Dana Whitfield: Part-time retail job, 1240.00 a month as reported (verified: no)."
    assert texts["case-2001"].endswith("Next step: Waiting for proof of income.")


def test_every_cited_id_matches_the_profile_pattern(db):
    names = [n for (n,) in sqlite3.connect(db).execute("SELECT name FROM applicants")]
    for name in names:
        for r in lookup(db, "supervisor", f"Tell me about {name}"):
            assert P.source_id_pattern.fullmatch(r["id"]), r["id"]


def _sql(db, monkeypatch, role):
    seen, real = [], sqlite3.connect

    def traced(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(server.sqlite3, "connect", traced)
    lookup(db, role)
    return " ".join(seen)


def test_phone_and_address_are_never_read(db, monkeypatch):
    """They are in the database but not in records.yaml."""
    sql = _sql(db, monkeypatch, "supervisor")
    assert '"phone"' not in sql and '"street_address"' not in sql


def test_intake_lookup_never_reads_income_or_payments(db, monkeypatch):
    sql = _sql(db, monkeypatch, "intake_worker")
    assert '"income"' not in sql and '"payments"' not in sql


# --- guardrails ------------------------------------------------------------------------

@pytest.mark.parametrize("text, category", [
    ("Is Tom Brandt eligible for emergency assistance?", "eligibility_decision"),
    ("Should we approve the application?", "eligibility_decision"),
    ("Does the household qualify for SNAP?", "eligibility_decision"),
    ("Is Lena Kovacs lying about her income?", "integrity_judgment"),
    ("Could this be fraud?", "integrity_judgment"),
    ("The client said he wants to end his life", "crisis"),
    ("She told me she is being abused at home", "crisis"),
])
def test_decisions_and_risks_are_blocked(text, category):
    d = check_input(text, P)
    assert not d.allowed and d.category == category


@pytest.mark.parametrize("text", [
    "What documents are still missing for Dana Whitfield?",
    "What is the status of Marcus Oyelaran's SNAP case?",
    "What income has Rosa Delgado reported?",
    "How long does a SNAP application usually take?",
    "Are college students eligible for SNAP?",          # a policy question, about no one
    "Who decides eligibility?",
])
def test_lookups_and_policy_questions_pass(text):
    assert check_input(text, P).allowed


@pytest.mark.parametrize("answer, category", [
    ("Dana Whitfield is not eligible for SNAP.", "eligibility_decision"),
    ("Rosa Delgado will likely be approved.", "eligibility_decision"),
    ("The case should be denied.", "eligibility_decision"),
    ("Tom Brandt might be lying.", "integrity_judgment"),
    ("This looks like possible fraud.", "integrity_judgment"),
    ("SNAP needs pay stubs for [INCOME_PERIOD].", "ungrounded"),
])
def test_answers_that_decide_or_accuse_are_blocked(answer, category):
    d = check_output(answer, P)
    assert not d.allowed and d.category == category


def test_every_office_policy_passes_the_answer_rules():
    """A policy quoted back by the model must never be blocked."""
    for e in json.loads(P.knowledge_base.read_text()):
        assert check_output(e["text"], P).allowed, e["id"]
        assert P.source_id_pattern.fullmatch(e["id"])
        assert e["status"].startswith("SAMPLE")


def test_example_buttons_show_both_answers_and_blocks():
    results = [check_input(e, P).allowed for e in P.examples]
    assert results == [True, True, True, True, False, False]


def test_role_reply_for_supervisors():
    assert "including supervisors" in P.response("eligibility_decision", "supervisor")
    assert P.response("eligibility_decision", "intake_worker") == P.responses["eligibility_decision"]