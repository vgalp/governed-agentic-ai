"""Human approval before changes: profile rules, proposals, approval state and the tool
that makes the change. The policy itself is tested with OPA: opa test policies/ -v"""

import importlib.util
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from agents import actions
from api.events import EVENT_TOPICS
from audit.chain import AUDIT_TOPICS
from profiles.loader import ProfileError, load_profile, load_profile_from

DATA = Path(__file__).resolve().parent.parent / "profiles" / "clinic-assistant" / "data"
_spec = importlib.util.spec_from_file_location("build_db", DATA / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)

server = pytest.importorskip("mcp_servers.appointments.server")
records = pytest.importorskip("mcp_servers.records.server")

P = load_profile("clinic-assistant")
ACTION = P.actions["reschedule_appointment"]
NOW = datetime(2026, 10, 7, 21, 0)          # a Wednesday evening
NOTES = [{"id": "apt-0001", "title": "Upcoming appointment",
          "text": "Chester Aufderhar: Follow-up visit on 2026-11-09 at 09:30 (scheduled)."},
         {"id": "med-9", "title": "Current medication", "text": "Chester Aufderhar: lisinopril 10 MG."}]


# --- the profile ---------------------------------------------------------------------

def test_clinic_has_the_reschedule_action():
    assert ACTION.tool == "reschedule_appointment" and ACTION.agent == "executor"
    assert set(ACTION.approve_roles) == {"clinician", "nurse"}
    assert "compliance" not in ACTION.propose_roles


def test_only_the_executor_holds_the_tool_that_makes_changes():
    assert "reschedule_appointment" in P.agents["executor"]
    assert "reschedule_appointment" not in P.agents["planner"]


def test_policy_data_has_the_action_rules():
    a = P.policy_data()["actions"]["reschedule_appointment"]
    assert a["approve_roles"] == ["clinician", "nurse"]
    assert a["allowed_times"]["field"] == "new_start"


def test_adhd_profile_has_no_actions():
    assert load_profile("adhd-assistant").actions == {}


@pytest.fixture
def clinic_copy(tmp_path):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(P.path, dst, ignore=shutil.ignore_patterns("clinic.db"))

    def edit(file, change):
        p = dst / file
        d = yaml.safe_load(p.read_text())
        change(d)
        p.write_text(yaml.safe_dump(d, sort_keys=False))
        return load_profile_from(dst)
    return edit


def test_the_planner_may_never_hold_the_change_tool(clinic_copy):
    with pytest.raises(ProfileError, match="talks to the model, so it may not hold"):
        clinic_copy("profile.yaml", lambda d: d["agents"]["planner"]["tools"].append("reschedule_appointment"))


def test_only_the_action_agent_may_hold_the_change_tool(clinic_copy):
    with pytest.raises(ProfileError, match="only 'executor' may hold"):
        clinic_copy("profile.yaml", lambda d: d["agents"].update(reporter={"tools": ["reschedule_appointment"]}))


def test_approvers_need_the_tool(clinic_copy):
    with pytest.raises(ProfileError, match="approver role 'nurse' must have"):
        clinic_copy("roles.yaml", lambda d: d["roles"]["nurse"]["tools"].remove("reschedule_appointment"))


def test_unknown_intent_pattern_is_refused(clinic_copy):
    with pytest.raises(ProfileError, match="intent"):
        clinic_copy("actions.yaml", lambda d: d["reschedule_appointment"].update(intent="nope"))


def test_unknown_role_is_refused(clinic_copy):
    with pytest.raises(ProfileError, match="unknown role 'manager'"):
        clinic_copy("actions.yaml", lambda d: d["reschedule_appointment"].update(approve_roles=["manager"]))


def test_allowed_times_must_use_a_datetime_field(clinic_copy):
    with pytest.raises(ProfileError, match="datetime field"):
        clinic_copy("actions.yaml", lambda d: d["reschedule_appointment"]["allowed_times"].update(field="appointment_id"))


def test_actions_need_their_replies(clinic_copy):
    with pytest.raises(ProfileError, match="action_pending"):
        clinic_copy("responses.yaml", lambda d: d.pop("action_pending"))


# --- spotting a change request ---------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Move Chester Aufderhar's next appointment to Friday at 10",
    "Please reschedule Jesse Wolf's appointment on November 9",
    "Can we postpone the visit for Chester Aufderhar to next week?",
    "Chester Aufderhar's appointment needs to be moved to 2026-11-13 10:00",
])
def test_change_requests_are_detected(text):
    assert actions.detect(P, text) is ACTION


@pytest.mark.parametrize("text", [
    "When is Chester Aufderhar's next appointment?",
    "What medications is Chester Aufderhar taking?",
    "What is our cancellation policy?",
])
def test_questions_are_not_change_requests(text):
    assert actions.detect(P, text) is None


def test_the_model_is_told_it_changes_nothing():
    system = actions.extraction_messages(ACTION, "- [apt-0001] ...", "Move it", NOW)[0]["content"]
    assert "never make the change" in system and "2026-10-07" in system
    assert '"appointment_id"' in system and '"new_start"' in system


# --- checking the model's proposal ---------------------------------------------------------

def test_proposal_is_read_from_the_reply():
    raw = 'Sure! {"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"} Let me know.'
    assert actions.parse_proposal(raw) == {"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"}


@pytest.mark.parametrize("raw, error", [
    ("I can't do that.", "did not return a proposal"),
    ("{not json}", "not valid JSON"),
    ('{"error": "no appointment on that day"}', "no appointment on that day"),
])
def test_bad_replies_become_reasons(raw, error):
    assert error in actions.parse_proposal(raw)


def test_valid_proposal():
    args, err = actions.validate(ACTION, {"appointment_id": "apt-0001", "new_start": "2026-11-13 10:00"},
                                 ["apt-0001", "med-9"])
    assert err is None and args == {"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"}


@pytest.mark.parametrize("proposal, error", [
    ({"appointment_id": "apt-0099", "new_start": "2026-11-13T10:00"}, "not in the records I was given"),
    ({"appointment_id": "med-9", "new_start": "2026-11-13T10:00"}, "does not look right"),
    ({"appointment_id": "apt-0001", "new_start": "next Friday"}, "not a date and time"),
    ({"appointment_id": "apt-0001"}, "has no new_start"),
])
def test_invalid_proposals_are_refused(proposal, error):
    args, err = actions.validate(ACTION, proposal, ["apt-0001", "med-9"])
    assert args is None and error in err


def test_facts_for_the_policy():
    f = actions.facts(ACTION, {"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"}, NOW)
    assert f == {"new_start": {"weekday": 4, "hour": 10, "minute": 0, "in_past": False}}
    past = actions.facts(ACTION, {"appointment_id": "apt-0001", "new_start": "2026-10-01T10:00"}, NOW)
    assert past["new_start"]["in_past"] is True


def test_summary_is_masked():
    text = actions.summary(ACTION, {"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"}, NOTES,
                           masker=lambda t: t.replace("Chester Aufderhar", "[PERSON_1]"))
    assert "Chester" not in text and "[PERSON_1]" in text
    assert text.startswith("Reschedule an appointment: apt-0001") and "to 2026-11-13 at 10:00" in text


def test_new_approval_expires():
    a = actions.new_approval(P, ACTION, {"appointment_id": "apt-0001", "new_start": "2026-11-13T10:00"},
                             "summary", "r1", "ui", "front_desk", ["clinician", "nurse"], NOW)
    assert a["approval_id"].startswith("ap-") and a["requester"] == "ui"
    assert a["expires_at"] == "2026-10-07T21:30:00"


# --- approval state, rebuilt from Kafka -----------------------------------------------------

def requested(aid="ap-1", expires="2026-10-07T21:30:00"):
    return {"approval_id": aid, "request_id": "r1", "action": "reschedule_appointment",
            "args": {}, "summary": "s", "approve_roles": ["nurse"], "requester": "ui",
            "created_at": "2026-10-07T21:00:00", "expires_at": expires}


def test_state_follows_the_topics():
    s = actions.ApprovalState()
    s.apply(actions.REQUESTED, requested())
    assert [a["approval_id"] for a in s.pending()] == ["ap-1"]
    s.apply(actions.DECIDED, {"approval_id": "ap-1", "decision": "approve", "approver": "dashboard",
                              "approver_role": "nurse", "decided_at": "2026-10-07T21:05:00"})
    assert s.approvals["ap-1"]["status"] == "approved" and not s.pending()
    s.apply(actions.EXECUTED, {"approval_id": "ap-1", "status": "done", "result": {"after": "x"}})
    assert s.approvals["ap-1"]["status"] == "done"


def test_a_second_decision_is_ignored():
    s = actions.ApprovalState()
    s.apply(actions.REQUESTED, requested())
    s.apply(actions.DECIDED, {"approval_id": "ap-1", "decision": "reject", "approver": "a", "approver_role": "nurse"})
    s.apply(actions.DECIDED, {"approval_id": "ap-1", "decision": "approve", "approver": "b", "approver_role": "nurse"})
    assert s.approvals["ap-1"]["status"] == "rejected"


def test_unknown_approval_is_ignored():
    assert actions.ApprovalState().apply(actions.DECIDED, {"approval_id": "nope", "decision": "approve"}) is None


def test_overdue_approvals():
    s = actions.ApprovalState()
    s.apply(actions.REQUESTED, requested("ap-1", "2026-10-07T21:30:00"))
    s.apply(actions.REQUESTED, requested("ap-2", "2026-10-07T22:30:00"))
    assert [a["approval_id"] for a in s.overdue(datetime(2026, 10, 7, 22, 0))] == ["ap-1"]


def test_topics_are_followed_and_audited():
    assert set(actions.APPROVAL_TOPICS) <= set(EVENT_TOPICS)
    assert "audit.approvals" in AUDIT_TOPICS


# --- the tool that makes the change -----------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return build_db.build(DATA / "sample_csv", tmp_path / "clinic.db")


def first_appointment(db):
    return sqlite3.connect(db).execute("SELECT id, starts_at FROM appointments ORDER BY id").fetchone()


def test_reschedule_changes_the_appointment(db):
    aid, before = first_appointment(db)
    r = server.reschedule(db, aid, "2026-11-13T10:00", "ap-1")
    assert r == {"status": "done", "appointment_id": aid, "before": before, "after": "2026-11-13T10:00"}
    assert first_appointment(db)[1] == "2026-11-13T10:00"


def test_the_same_approval_changes_nothing_twice(db):
    aid, _ = first_appointment(db)
    server.reschedule(db, aid, "2026-11-13T10:00", "ap-1")
    again = server.reschedule(db, aid, "2026-11-20T11:00", "ap-1")
    assert again["status"] == "already_done"
    assert first_appointment(db)[1] == "2026-11-13T10:00"


def test_every_change_is_logged_with_its_approval(db):
    aid, before = first_appointment(db)
    server.reschedule(db, aid, "2026-11-13T10:00", "ap-1")
    row = sqlite3.connect(db).execute("SELECT approval_id, target, before, after FROM action_log").fetchone()
    assert row == ("ap-1", aid, before, "2026-11-13T10:00")


@pytest.mark.parametrize("aid, start, approval, reason", [
    ("apt-9999", "2026-11-13T10:00", "ap-1", "not found"),
    (None, "Friday", "ap-1", "not a date and time"),
    (None, "2026-11-13T10:00", "", "no approval ID"),
])
def test_bad_changes_are_refused(db, aid, start, approval, reason):
    aid = aid or first_appointment(db)[0]
    r = server.reschedule(db, aid, start, approval)
    assert r["status"] == "failed" and reason in r["reason"]


def test_records_show_the_new_time(db):
    aid, _ = first_appointment(db)
    pid = sqlite3.connect(db).execute("SELECT patient_id FROM appointments WHERE id = ?", (aid,)).fetchone()[0]
    name = sqlite3.connect(db).execute("SELECT name FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    server.reschedule(db, aid, "2026-11-13T10:00", "ap-1")
    found = records.search(db, f"When is {name}'s appointment?", ["patient_details", "appointments"],
                           P.records_map)["results"]
    assert any(r["id"] == aid and "2026-11-13 at 10:00" in r["text"] for r in found)