"""The tool registry (tools.yaml): one place that describes every tool a profile can use,
and the rules the loader enforces so a profile can't give itself unsafe tool access."""

import shutil

import pytest
import yaml

from gateway import gateway
from profiles.loader import ProfileError, load_profile, load_profile_from


@pytest.fixture
def clinic_copy(tmp_path):
    """A copy of the clinic profile. edit(file, change) changes one YAML file and reloads."""
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(load_profile("clinic-assistant").path, dst,
                    ignore=shutil.ignore_patterns("clinic.db", "sample_csv"))

    def edit(name, change):
        p = dst / name
        d = yaml.safe_load(p.read_text())
        change(d)
        p.write_text(yaml.safe_dump(d, sort_keys=False))
        return load_profile_from(dst)
    return edit


# --- what the registry holds ---------------------------------------------------

def test_clinic_tools_are_described_in_one_place():
    p = load_profile("clinic-assistant")
    assert set(p.tools) == {"search_knowledge", "search_records", "reschedule_appointment"}
    records = p.tools["search_records"]
    assert records.server == "http://127.0.0.1:8101/mcp" and records.kind == "read"
    assert records.receives_identifiers and records.role_filtered
    assert p.tools["reschedule_appointment"].kind == "change"


def test_old_settings_are_derived_from_the_registry():
    p = load_profile("clinic-assistant")
    assert p.tool_data_labels == {"search_knowledge": "approved_content", "search_records": "patient_record",
                                  "reschedule_appointment": "patient_record"}
    assert p.identifier_tools == ("search_records",)
    assert p.role_filtered_tools == ("search_records",)


def test_policy_data_names_each_tool_kind_and_the_change_owner():
    tools = load_profile("clinic-assistant").policy_data()["tools"]
    assert tools["search_records"] == {"kind": "read"}
    assert tools["reschedule_appointment"] == {"kind": "change", "agent": "executor"}


def test_gateway_uses_the_profile_server(monkeypatch):
    """The server URL comes from tools.yaml, not from code."""
    p = load_profile("adhd-assistant")
    seen = {}

    async def fake_call(url, tool, args):
        seen["url"] = url
        return {"results": []}
    monkeypatch.setattr(gateway, "_call", fake_call)
    monkeypatch.setattr(gateway, "_is_allowed", lambda *a: True)
    monkeypatch.setattr(gateway, "_audit", lambda e: None)
    gateway.call_tool("planner", "search_knowledge", {"query": "x"}, "r1", p)
    assert seen["url"] == "http://127.0.0.1:8100/mcp"


def test_gateway_denies_a_tool_not_in_the_registry(monkeypatch):
    events = []
    monkeypatch.setattr(gateway, "_is_allowed", lambda *a: True)     # even if policy said yes
    monkeypatch.setattr(gateway, "_audit", events.append)
    with pytest.raises(gateway.ToolCallDenied):
        gateway.call_tool("planner", "delete_everything", {}, "r1", load_profile("adhd-assistant"))
    assert events[-1]["decision"] == "deny"


# --- what the loader refuses ------------------------------------------------------

def test_granted_tool_must_be_registered(clinic_copy):
    with pytest.raises(ProfileError, match="'search_records' is not in tools.yaml"):
        clinic_copy("tools.yaml", lambda d: d.pop("search_records"))


def test_planner_may_never_hold_a_change_tool(clinic_copy):
    with pytest.raises(ProfileError, match="talks to the model, so it may not hold 'reschedule_appointment'"):
        clinic_copy("profile.yaml", lambda d: d["agents"]["planner"]["tools"].append("reschedule_appointment"))


def test_an_action_can_not_run_from_the_planner(clinic_copy):
    with pytest.raises(ProfileError, match="may never make a change"):
        clinic_copy("actions.yaml", lambda d: d["reschedule_appointment"].update(agent="planner"))


def test_a_change_tool_needs_an_action(clinic_copy):
    """No action means nothing would ask a person first."""
    def add_unused_change_tool(d):
        d["agents"]["executor"]["tools"].append("cancel_appointment")
    tools = load_profile("clinic-assistant").path / "tools.yaml"
    reg = yaml.safe_load(tools.read_text())
    reg["cancel_appointment"] = {"server": "http://127.0.0.1:8103/mcp", "kind": "change",
                                 "data_label": "patient_record"}
    clinic_copy("tools.yaml", lambda d: d.update(reg))   # registered, not granted: fine
    with pytest.raises(ProfileError, match="no action in actions.yaml uses it"):
        clinic_copy("profile.yaml", add_unused_change_tool)


def test_an_action_must_use_a_change_tool(clinic_copy):
    def use_read_tool(d):
        d["reschedule_appointment"]["tool"] = "search_records"
    with pytest.raises(ProfileError, match="must be a change tool"):
        clinic_copy("actions.yaml", use_read_tool)


def test_identifiers_only_go_to_tools_whose_data_stays_local(clinic_copy):
    with pytest.raises(ProfileError, match="receives identifiers"):
        clinic_copy("tools.yaml", lambda d: d["search_knowledge"].update(receives_identifiers=True))


def test_change_tools_get_no_identifiers_or_role_filter(clinic_copy):
    with pytest.raises(ProfileError, match="for read tools"):
        clinic_copy("tools.yaml", lambda d: d["reschedule_appointment"].update(receives_identifiers=True))


def test_role_filter_needs_roles(tmp_path):
    dst = tmp_path / "adhd-assistant"
    shutil.copytree(load_profile("adhd-assistant").path, dst)
    p = dst / "tools.yaml"
    d = yaml.safe_load(p.read_text())
    d["search_knowledge"]["role_filtered"] = True
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ProfileError, match="has no roles"):
        load_profile_from(dst)


@pytest.mark.parametrize("change, message", [
    (lambda t: t.update(server="ftp://x"), "http:// or https://"),
    (lambda t: t.pop("server"), "missing 'server'"),
    (lambda t: t.update(kind="write"), "kind must be read or change"),
    (lambda t: t.pop("kind"), "missing 'kind'"),
    (lambda t: t.update(lane="router"), "lane must be one of"),
    (lambda t: t.update(role_filtered="yes"), "must be true or false"),
])
def test_bad_tool_entries_are_refused(clinic_copy, change, message):
    with pytest.raises(ProfileError, match=message):
        clinic_copy("tools.yaml", lambda d: change(d["search_knowledge"]))


def test_change_tool_lane_is_always_action(clinic_copy):
    with pytest.raises(ProfileError, match="always shows under the 'action' lane"):
        clinic_copy("tools.yaml", lambda d: d["reschedule_appointment"].update(lane="records"))


def test_old_tool_settings_point_to_tools_yaml(clinic_copy):
    with pytest.raises(ProfileError, match="moved to tools.yaml"):
        clinic_copy("profile.yaml", lambda d: d.update(tool_data_labels={"search_knowledge": "x"}))
    with pytest.raises(ProfileError, match="moved to tools.yaml"):
        clinic_copy("roles.yaml", lambda d: d.update(filtered_tools=["search_records"]))