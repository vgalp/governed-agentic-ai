"""Records search over the synthetic sample, and the rules for tools that get real names."""

import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from profiles.loader import ProfileError, load_profile, load_profile_from

DATA = Path(__file__).resolve().parent.parent / "profiles" / "clinic-assistant" / "data"
_spec = importlib.util.spec_from_file_location("build_db", DATA / "build_db.py")
build_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_db)

server = pytest.importorskip("mcp_servers.records.server")
ALL = list(load_profile("clinic-assistant").data_classes)      # everything: the clinician's view


@pytest.fixture
def db(tmp_path):
    return build_db.build(DATA / "sample_csv", tmp_path / "clinic.db")


def names(db):
    return [n for (n,) in sqlite3.connect(db).execute("SELECT name FROM patients ORDER BY name")]


def test_full_name_finds_the_patient_with_cited_records(db):
    name = names(db)[0]
    results = server.search(db, f"What medications is {name} taking?", ALL)["results"]
    assert results and results[0]["id"].startswith("pt-")
    assert all(name in r["text"] for r in results)                 # only that patient's records
    assert all(r["id"].split("-")[0] in {"pt", "apt", "med", "enc", "clm"} for r in results)


def test_no_name_means_no_records(db):
    assert server.search(db, "What is our cancellation policy?", ALL)["results"] == []


def test_unknown_name_means_no_records(db):
    assert server.search(db, "When is Nobody Atall's appointment?", ALL)["results"] == []


def test_missing_database_is_not_an_error(tmp_path):
    r = server.search(tmp_path / "missing.db", "anything", ALL)
    assert r["results"] == [] and "no records database" in r["note"]


def test_database_is_opened_read_only(db):
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM patients")


# --- profile rules for tools that receive identifiers ------------------------

def test_clinic_profile_gives_names_only_to_the_records_tool():
    p = load_profile("clinic-assistant")
    assert p.identifier_tools == ("search_records",)
    assert p.tool_data_labels["search_records"] in p.routing["never_external_labels"]


@pytest.fixture
def copy_of_clinic(tmp_path):
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(load_profile("clinic-assistant").path, dst,
                    ignore=shutil.ignore_patterns("clinic.db", "sample_csv"))
    return dst


def _edit(path, change):
    d = yaml.safe_load(path.read_text())
    change(d)
    path.write_text(yaml.safe_dump(d, sort_keys=False))


def test_identifier_tool_must_return_never_external_data(copy_of_clinic):
    _edit(copy_of_clinic / "profile.yaml", lambda d: d["routing"].update(never_external_labels=[]))
    with pytest.raises(ProfileError, match="must be in routing.never_external_labels"):
        load_profile_from(copy_of_clinic)


def test_identifier_tool_must_be_granted(copy_of_clinic):
    _edit(copy_of_clinic / "profile.yaml", lambda d: d["agents"]["planner"].update(tools=["search_knowledge"]))
    with pytest.raises(ProfileError, match="not granted"):
        load_profile_from(copy_of_clinic)