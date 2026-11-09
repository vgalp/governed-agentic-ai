"""The directory (ADR 029): Keycloak, Entra ID and Okta read the same way (groups and members,
paged, bounded), secrets only from the environment, and the access report gives each person the
same role sign-in would."""

import pytest

from auth import directory as d
from auth.directory import CachedDirectory, DirectoryError, Entra, Group, Keycloak, Member, Okta, access_report
from auth.sign_in import role_for
from profiles.loader import ProfileError, load_profile, load_profile_from


class Resp:
    def __init__(self, status=200, body=None, links=None):
        self.status_code, self._body, self.links = status, body, links or {}
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body


class FakeSession:
    """Answers by URL prefix; records each call (URL, params, headers)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, url, params):
        for prefix, answer in self.routes:
            if url.startswith(prefix):
                return answer(url, params or {}) if callable(answer) else answer
        return Resp(404, {})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        return self._answer(url, params)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data, None))
        return self._answer(url, data)


# --- Keycloak ----------------------------------------------------------------------------------------

KC = {"provider": "keycloak", "url": "http://127.0.0.1:8180", "realm": "governed-demo",
      "client_id": "governed-directory", "secret_env": "KC_DIR_SECRET"}


def test_keycloak_reads_groups_and_members_with_a_service_sign_in(monkeypatch):
    monkeypatch.setenv("KC_DIR_SECRET", "s3cret")
    admin = "http://127.0.0.1:8180/admin/realms/governed-demo"
    s = FakeSession([
        ("http://127.0.0.1:8180/realms/governed-demo/protocol/openid-connect/token", Resp(200, {"access_token": "T"})),
        (f"{admin}/groups/g1/members", Resp(200, [{"id": "u1", "username": "dr.lee", "firstName": "Dana", "lastName": "Lee"}])),
        (f"{admin}/groups/g2/members", Resp(200, [])),
        (f"{admin}/groups", Resp(200, [{"id": "g1", "name": "clinicians"}, {"id": "g2", "name": "nurses"}])),
    ])
    groups = Keycloak(KC, s).groups()
    assert groups == [Group("g1", "clinicians", (Member("u1", "dr.lee", "Dana Lee"),)), Group("g2", "nurses", ())]
    token_call = s.calls[0]
    assert token_call[2]["grant_type"] == "client_credentials" and token_call[2]["client_secret"] == "s3cret"
    assert all(c[3] == {"Authorization": "Bearer T"} for c in s.calls[1:])


def test_keycloak_pages_through_large_groups(monkeypatch):
    monkeypatch.setenv("KC_DIR_SECRET", "x")
    admin = "http://127.0.0.1:8180/admin/realms/governed-demo"

    def members(url, params):
        first = params["first"]
        n = 100 if first < 200 else 7
        return Resp(200, [{"id": f"u{first + i}", "username": f"u{first + i}"} for i in range(n)])
    s = FakeSession([("http://127.0.0.1:8180/realms/", Resp(200, {"access_token": "T"})),
                     (f"{admin}/groups/g1/members", members),
                     (f"{admin}/groups", Resp(200, [{"id": "g1", "name": "nurses"}]))])
    assert len(Keycloak(KC, s).groups()[0].members) == 207      # 100 + 100 + 7, three pages


def test_a_missing_secret_or_a_refused_sign_in_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("KC_DIR_SECRET", raising=False)
    with pytest.raises(DirectoryError, match="KC_DIR_SECRET"):
        Keycloak(KC)
    monkeypatch.setenv("KC_DIR_SECRET", "wrong")
    s = FakeSession([("http://127.0.0.1:8180/realms/", Resp(401, {"error": "unauthorized_client"}))])
    with pytest.raises(DirectoryError, match="refused the service sign-in") as e:
        Keycloak(KC, s).groups()
    assert "wrong" not in str(e.value)


# --- Entra ID ------------------------------------------------------------------------------------------

ENTRA = {"provider": "entra", "tenant_id": "tid", "client_id": "app", "secret_env": "ENTRA_SECRET"}
GRAPH = "https://graph.microsoft.com/v1.0"


def test_entra_follows_graph_next_links_and_reads_user_members(monkeypatch):
    monkeypatch.setenv("ENTRA_SECRET", "e")
    s = FakeSession([
        ("https://login.microsoftonline.com/tid/oauth2/v2.0/token", Resp(200, {"access_token": "G"})),
        (f"{GRAPH}/groups/aaa/members", Resp(200, {"value": [{"id": "1", "displayName": "Dana Lee", "userPrincipalName": "dana@clinic.org"}]})),
        (f"{GRAPH}/groups/bbb/members", Resp(200, {"value": []})),
        (f"{GRAPH}/groups?page2", Resp(200, {"value": [{"id": "bbb", "displayName": "Nurses"}]})),
        (f"{GRAPH}/groups?", Resp(200, {"value": [{"id": "aaa", "displayName": "Clinicians"}], "@odata.nextLink": f"{GRAPH}/groups?page2"})),
    ])
    groups = Entra(ENTRA, s).groups()
    assert [(g.id, g.name, len(g.members)) for g in groups] == [("aaa", "Clinicians", 1), ("bbb", "Nurses", 0)]
    assert groups[0].members[0] == Member("1", "dana@clinic.org", "Dana Lee")
    assert s.calls[0][2]["scope"] == "https://graph.microsoft.com/.default"


def test_entra_can_be_limited_to_named_groups(monkeypatch):
    monkeypatch.setenv("ENTRA_SECRET", "e")
    s = FakeSession([
        ("https://login.microsoftonline.com/", Resp(200, {"access_token": "G"})),
        (f"{GRAPH}/groups/aaa/members", Resp(200, {"value": []})),
        (f"{GRAPH}/groups?", Resp(200, {"value": [{"id": "aaa", "displayName": "A"}, {"id": "zzz", "displayName": "Z"}]})),
    ])
    groups = Entra({**ENTRA, "groups": ["aaa"]}, s).groups()
    assert [g.id for g in groups] == ["aaa"]
    assert not any("/groups/zzz/" in c[1] for c in s.calls)        # never read the members of other groups


# --- Okta ----------------------------------------------------------------------------------------------

def test_okta_uses_an_api_token_follows_link_headers_and_skips_deactivated_people(monkeypatch):
    monkeypatch.setenv("OKTA_TOKEN", "o")
    org = "https://clinic.okta.com"
    s = FakeSession([
        (f"{org}/api/v1/groups/00g1/users", Resp(200, [
            {"id": "1", "status": "ACTIVE", "profile": {"login": "sam@clinic.org", "firstName": "Sam", "lastName": "Kim"}},
            {"id": "2", "status": "DEPROVISIONED", "profile": {"login": "gone@clinic.org"}}])),
        (f"{org}/api/v1/groups?after=x", Resp(200, [])),
        (f"{org}/api/v1/groups", Resp(200, [{"id": "00g1", "profile": {"name": "nurses"}}],
                                      links={"next": {"url": f"{org}/api/v1/groups?after=x"}})),
    ])
    groups = Okta({"provider": "okta", "org_url": org + "/", "token_env": "OKTA_TOKEN"}, s).groups()
    assert groups == [Group("00g1", "nurses", (Member("1", "sam@clinic.org", "Sam Kim"),))]
    assert s.calls[0][3]["Authorization"] == "SSWS o"


def test_an_unreachable_directory_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("OKTA_TOKEN", "o")

    class Down:
        def get(self, *a, **k):
            raise d.requests.ConnectionError("x")
    cache = CachedDirectory({"provider": "okta", "org_url": "https://x.okta.com", "token_env": "OKTA_TOKEN"})
    monkeypatch.setattr(d, "connect", lambda cfg: Okta(cfg, Down()))
    groups, error = cache.groups()
    assert groups is None and "did not answer" in error


def test_the_directory_is_asked_once_per_cache_period(monkeypatch):
    calls = []

    class One:
        def groups(self):
            calls.append(1)
            return []
    now = [0.0]
    monkeypatch.setattr(d, "connect", lambda cfg: One())
    cache = CachedDirectory({"provider": "okta"}, seconds=300, clock=lambda: now[0])
    cache.groups(), cache.groups()
    now[0] = 301
    cache.groups()
    assert len(calls) == 2


# --- the access report ---------------------------------------------------------------------------------

ROLES = {"clinicians": "clinician", "nurses": "nurse", "front-desk": "front_desk"}
TITLES = {"clinician": "Clinician", "nurse": "Nurse", "front_desk": "Front desk"}


def test_the_report_gives_each_person_the_role_sign_in_would():
    dana, sam, pat = Member("1", "dr.lee", "Dana Lee"), Member("2", "nurse.kim", "Sam Kim"), Member("3", "pat", "Pat Doe")
    groups = [Group("g2", "nurses", (sam, pat)), Group("g1", "clinicians", (dana, pat)), Group("g9", "volunteers", (Member("4", "v", "V"),))]
    r = access_report(groups, ROLES, TITLES)
    roles = {p["username"]: p["role"] for p in r["people"]}
    assert roles == {"dr.lee": "clinician", "nurse.kim": "nurse", "pat": "clinician"}
    assert roles["pat"] == role_for(("nurses", "clinicians"), ROLES)          # same rule as sign-in
    assert any("Pat Doe is in clinicians, nurses" in w for w in r["warnings"])
    assert any("front-desk" in w for w in r["warnings"])                     # mapped, but no such group
    assert r["unmapped_groups"] == ["volunteers"]
    assert "v" not in roles                                                   # no mapped group, not listed


def test_mapping_keys_may_be_group_ids_as_entra_tokens_carry():
    groups = [Group("8f1c-guid", "Clinic Nurses", (Member("1", "sam@clinic.org", "Sam Kim"),))]
    r = access_report(groups, {"8f1c-guid": "nurse"}, TITLES)
    assert r["mapping"][0]["found"] and r["mapping"][0]["directory_name"] == "Clinic Nurses"
    assert r["people"][0]["role"] == "nurse" and r["unmapped_groups"] == []


def test_without_a_directory_the_report_is_the_mapping_only():
    r = access_report(None, ROLES, TITLES)
    assert r["people"] is None and [m["role_title"] for m in r["mapping"]] == ["Clinician", "Nurse", "Front desk"]


# --- the profile ---------------------------------------------------------------------------------------

def test_the_clinic_profile_reads_the_local_keycloak_and_shows_names_to_compliance_only():
    s = load_profile("clinic-assistant").sign_in
    assert s["directory"]["provider"] == "keycloak" and s["admin_roles"] == ["compliance"]


def _profile_with(tmp_path, old, new):
    import shutil
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(load_profile("clinic-assistant").path, dst)
    f = dst / "profile.yaml"
    text = f.read_text()
    assert old in text
    f.write_text(text.replace(old, new))
    return dst


def test_a_secret_pasted_where_its_variable_name_belongs_is_refused(tmp_path):
    dst = _profile_with(tmp_path, "secret_env: KEYCLOAK_DIRECTORY_SECRET", "secret_env: hX9k2-real-secret")
    with pytest.raises(ProfileError, match="NAME of an environment variable"):
        load_profile_from(dst)


def test_a_directory_without_its_settings_is_refused(tmp_path):
    dst = _profile_with(tmp_path, "    realm: governed-demo\n", "")
    with pytest.raises(ProfileError, match="realm"):
        load_profile_from(dst)


def test_admin_roles_must_be_roles_of_the_profile(tmp_path):
    dst = _profile_with(tmp_path, "admin_roles: [compliance]", "admin_roles: [it_admin]")
    with pytest.raises(ProfileError, match="it_admin"):
        load_profile_from(dst)


def test_only_admin_roles_see_who_is_in_which_group():
    from auth.sign_in import Caller, SignIn
    s = SignIn({"mode": "oidc", "issuer": "http://127.0.0.1:8180/realms/x", "audience": "a", "client_id": "c",
                "roles": {"compliance": "compliance"}, "admin_roles": ["compliance"]}, env={})
    assert s.may_see_people(Caller("a", "a", "compliance"))
    assert not s.may_see_people(Caller("b", "b", "nurse")) and not s.may_see_people(None)
    assert SignIn(None, env={}).may_see_people(None)          # demo, one machine


def test_the_access_page_loads_sign_in_and_shows_text_safely():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "ui" / "access.html").read_text()
    assert '<script src="/auth.js"></script>' in html and "innerHTML" not in html
