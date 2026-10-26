"""Tool-use accuracy: the planner's routing, the scoring, and the shape of the frozen set.
The set itself is scored once, by eval/run_tool_use.py, not here."""

import json

import pytest

from agents.tool_choice import route
from eval import run_tool_use as tu
from profiles.loader import load_profile

CLINIC = load_profile("clinic-assistant")


def test_a_medication_review_goes_to_its_tool_only_for_clinicians():
    q = "Do any of Doris Whitfield's medications interact with each other?"
    assert route(CLINIC, "planner", "clinician", q) == {"path": "tool:medication_review", "allowed": True,
                                                        "tools": ["medication_review"]}
    nurse = route(CLINIC, "planner", "nurse", q)
    assert nurse["path"] == "tool:medication_review" and not nurse["allowed"] and nurse["tools"] == []


def test_a_change_request_becomes_a_proposal_for_roles_that_may_ask():
    q = "Please move Harold Brinkley's appointment on Friday to next Monday at 10."
    assert route(CLINIC, "planner", "front_desk", q)["path"] == "change:reschedule_appointment"
    assert route(CLINIC, "planner", "front_desk", q)["allowed"]
    assert not route(CLINIC, "planner", "compliance", q)["allowed"]


def test_general_questions_get_the_tools_the_role_may_use():
    q = "What is our cancellation policy?"
    assert route(CLINIC, "planner", "front_desk", q)["tools"] == ["search_knowledge", "search_records"]
    assert route(CLINIC, "planner", "compliance", q)["tools"] == ["search_knowledge"]
    assert route(CLINIC, "planner", None, q) == {"path": "general", "allowed": False, "tools": []}


def test_a_profile_without_roles_allows_its_tools():
    adhd = load_profile("adhd-assistant")
    assert route(adhd, "planner", None, "Help me plan tomorrow") == {
        "path": "general", "allowed": True, "tools": ["search_knowledge"]}


def row(expected, got, kind="plain", allowed=True, got_allowed=True):
    return {"id": "x", "profile": "p", "role": "r", "text": "t", "kind": kind, "expected_path": expected,
            "expected_allowed": allowed, "path": got, "allowed": got_allowed,
            "path_ok": expected == got, "allowed_ok": allowed == got_allowed}


def test_scores_separate_misses_false_triggers_and_permissions():
    rows = [row("tool:a", "tool:a"), row("tool:a", "general", "paraphrase"), row("general", "tool:a", "near_miss"),
            row("general", "general"), row("change:c", "change:c", "role", allowed=False, got_allowed=True)]
    s = tu.score(rows)
    assert (s["missed"], s["false_trigger"], s["wrong_permission"]) == (1, 1, 1)
    assert s["all"]["routing"]["k"] == 3 and s["all"]["both"]["k"] == 2
    assert (s["per_path"]["tool:a"]["precision"]["k"], s["per_path"]["tool:a"]["precision"]["n"]) == (1, 2)
    assert (s["per_path"]["tool:a"]["recall"]["k"], s["per_path"]["tool:a"]["recall"]["n"]) == (1, 2)
    assert s["by_kind"]["near_miss"]["routing"]["k"] == 0
    md = tu.table(s, rows)
    assert "False triggers" in md and "| x | role |" in md


def test_run_uses_the_planners_routing():
    rows = tu.run([{"id": "a", "profile": "clinic-assistant", "role": "clinician", "kind": "plain",
                    "text": "Run a medication review for Harold Brinkley.",
                    "expected_path": "tool:medication_review", "expected_allowed": True}])
    assert rows[0]["path_ok"] and rows[0]["allowed_ok"]


def test_freeze_is_read_from_the_protocol(tmp_path):
    p = tmp_path / "protocol.md"
    p.write_text("- `tool_use_v1.jsonl` SHA-256: pending\n")
    assert tu.frozen_hash(p) is None
    p.write_text("- `tool_use_v1.jsonl` SHA-256: " + "cd" * 32 + "\n")
    assert tu.frozen_hash(p) == "cd" * 32


def test_config_names_the_routing_files_and_code():
    a = tu.config_id(["clinic-assistant", "county-benefits"])
    assert len(a) == 12 and a == tu.config_id(["county-benefits", "clinic-assistant"])
    assert a != tu.config_id(["clinic-assistant"])


ITEMS = [json.loads(line) for line in tu.DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_set_is_well_formed():
    assert len(ITEMS) == 61 and len({i["id"] for i in ITEMS}) == 61
    for it in ITEMS:
        p = load_profile(it["profile"])
        assert it["role"] in p.roles, it["id"]
        assert it["kind"] in tu.KINDS, it["id"]
        kind, _, name = it["expected_path"].partition(":")
        if kind == "tool":
            assert name in p.agents["planner"] and p.tools[name].when, it["id"]
        elif kind == "change":
            assert name in p.actions, it["id"]
        else:
            assert it["expected_path"] == "general", it["id"]
        if it["kind"] == "role":
            assert it["expected_allowed"] is False, it["id"]


def test_once_frozen_the_set_matches_its_hash():
    if tu.frozen_hash() is None:
        pytest.skip("set not frozen yet")
    assert tu.sha256(tu.DATASET) == tu.frozen_hash()


def test_the_dashboard_gets_the_latest_run_and_whether_it_is_current(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from api import main
    monkeypatch.setattr(tu, "RESULTS", tmp_path)
    assert main.evaluations() == {"tool_use": None}
    rows = [row("general", "general")]
    rows[0]["profile"] = "clinic-assistant"
    (tmp_path / "tool_use_v1_abc.json").write_text(json.dumps({
        "set": "tool_use_v1.jsonl", "config": "abc", "run_at": "2026-10-26T10:00:00",
        "summary": tu.score(rows), "rows": rows}))
    got = main.evaluations()["tool_use"]
    assert got["both"]["k"] == 1 and got["current"] is False          # "abc" is not today's configuration
