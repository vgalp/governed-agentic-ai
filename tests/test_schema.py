"""Profile file schemas (ADR 026): the shape of each file is checked first, with a field path; an
unknown key is an error, not a silent no-op; the JSON Schema files in docs/schemas/ are current."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from profiles import schema
from profiles.loader import ProfileError, load_profile, load_profile_from

ROOT = Path(__file__).resolve().parent.parent


def copy_profile(name, tmp_path):
    dst = tmp_path / name
    shutil.copytree(load_profile(name).path, dst, ignore=shutil.ignore_patterns("*.db", "__pycache__"))
    return dst


def rewrite(dst, file, change):
    y = yaml.safe_load((dst / file).read_text())
    change(y)
    (dst / file).write_text(yaml.safe_dump(y, sort_keys=False))


def test_every_shipped_profile_passes_its_schemas():
    for p in sorted((ROOT / "profiles").iterdir()):
        if (p / "profile.yaml").exists():
            load_profile(p.name)


def test_a_misspelled_key_is_an_error_with_its_path_not_a_silent_no_op(tmp_path):
    dst = copy_profile("general-assistant", tmp_path)

    def typo(y):
        y["web_research"]["on_emtpy"] = y["web_research"].pop("on_empty")
    rewrite(dst, "tools.yaml", typo)
    with pytest.raises(ProfileError, match=r"tools.yaml: web_research: on_emtpy: unknown key \(misspelled\?\)"):
        load_profile_from(dst)


def test_a_wrong_type_and_a_missing_key_name_the_field(tmp_path):
    dst = copy_profile("clinic-assistant", tmp_path)
    rewrite(dst, "profile.yaml", lambda y: y["routing"].__setitem__("external_allowed", "yes"))
    with pytest.raises(ProfileError, match="profile.yaml: routing.external_allowed: Input should be a valid boolean"):
        load_profile_from(dst)
    dst = copy_profile("clinic-assistant", tmp_path / "b")
    rewrite(dst, "roles.yaml", lambda y: y["roles"]["nurse"].pop("title"))
    with pytest.raises(ProfileError, match="roles.yaml: roles.nurse.title: Field required"):
        load_profile_from(dst)


def test_an_enum_value_is_listed_in_the_message(tmp_path):
    dst = copy_profile("clinic-assistant", tmp_path)
    rewrite(dst, "tools.yaml", lambda y: y["search_records"].__setitem__("kind", "write"))
    with pytest.raises(ProfileError, match="search_records: kind: Input should be 'read' or 'change'"):
        load_profile_from(dst)


def test_dangerous_keys_get_a_hint():
    with pytest.raises(schema.SchemaError, match="never put an API key in a profile"):
        schema.validate(schema.Model, {"provider": "openai", "name": "x", "location": "external", "api_key": "sk-1"}, "m")


def test_the_category_name_with_a_comma_is_quoted_now():
    """The schema found this: an unquoted 'suicide, self-harm or abuse risk' split into a stray key."""
    cats = load_profile("general-assistant").classifier.categories
    assert any(c.name == "suicide, self-harm or abuse risk" for c in cats)


def test_the_json_schema_files_are_current():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "profile_schema.py"), "--check"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (ROOT / "docs" / "schemas" / "profile.schema.json").exists()
