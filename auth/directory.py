"""Read the organization's directory (ADR 029): its groups and their members, from Keycloak, Microsoft
Entra ID (Microsoft Graph) or Okta, so the access page can show who will get which role here.

Read-only and nothing stored: sign-in itself never needs this (a person's groups arrive in their
token, ADR 028). The directory is asked when someone opens the access page, and the answer is
cached for a few minutes. Credentials come from environment variables named in the profile's
`sign_in.directory`; they are never in a file in the repository.

    python -m auth.directory [profile]     print the access report in the terminal

Each provider returns the same shape: [{"id", "name", "members": [{"id", "username", "name"}]}].
"""

import os
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

MAX_GROUPS = 500          # bounds one listing: a large tenant is shown in part, never hangs the page
MAX_MEMBERS = 2000        # per group
PAGE = 100
TIMEOUT = 10


class DirectoryError(Exception):
    """The directory could not be read; the message says why, without secrets."""


@dataclass(frozen=True)
class Member:
    id: str
    username: str
    name: str


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    members: tuple[Member, ...]


def _env(name: str | None, what: str) -> str:
    value = os.environ.get(name or "")
    if not value:
        raise DirectoryError(f"set {name} ({what}) to read the directory")
    return value


class _Http:
    """GET and POST with a timeout; errors become DirectoryError with the status, never the body
    (which may echo a token)."""

    def __init__(self, session=None):
        self.s = session or requests.Session()

    def get(self, url, headers=None, params=None):
        try:
            r = self.s.get(url, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise DirectoryError(f"the directory did not answer ({type(e).__name__})") from None
        if r.status_code != 200:
            raise DirectoryError(f"the directory answered {r.status_code} for {url.split('?')[0]}")
        return r

    def token(self, url, data):
        try:
            r = self.s.post(url, data=data, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise DirectoryError(f"the directory's sign-in did not answer ({type(e).__name__})") from None
        if r.status_code != 200 or "access_token" not in (r.json() if r.content else {}):
            raise DirectoryError(f"the directory refused the service sign-in ({r.status_code}): check the client "
                                 "and its secret")
        return r.json()["access_token"]


class Keycloak:
    """Keycloak's admin REST API, signed in as a confidential client with a service account that has
    realm-management roles view-users and query-groups."""

    def __init__(self, cfg: dict, session=None):
        self.base = cfg["url"].rstrip("/")
        self.realm = cfg["realm"]
        self.client_id = cfg["client_id"]
        self.secret = _env(cfg.get("secret_env"), "the directory client's secret")
        self.http = _Http(session)

    def groups(self) -> list[Group]:
        token = self.http.token(f"{self.base}/realms/{self.realm}/protocol/openid-connect/token",
                                {"grant_type": "client_credentials", "client_id": self.client_id,
                                 "client_secret": self.secret})
        h = {"Authorization": f"Bearer {token}"}
        admin = f"{self.base}/admin/realms/{quote(self.realm)}"
        out, first = [], 0
        while len(out) < MAX_GROUPS:
            page = self.http.get(f"{admin}/groups", h, {"first": first, "max": PAGE, "briefRepresentation": "true"}).json()
            for g in page:
                out.append(self._members(admin, h, g))
            if len(page) < PAGE:
                break
            first += PAGE
        return out[:MAX_GROUPS]

    def _members(self, admin, h, g) -> Group:
        members, first = [], 0
        while len(members) < MAX_MEMBERS:
            page = self.http.get(f"{admin}/groups/{g['id']}/members", h, {"first": first, "max": PAGE}).json()
            for u in page:
                full = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x)
                members.append(Member(u["id"], u.get("username", ""), full or u.get("username", "")))
            if len(page) < PAGE:
                break
            first += PAGE
        return Group(g["id"], g.get("name", g["id"]), tuple(members[:MAX_MEMBERS]))


class Entra:
    """Microsoft Entra ID through Microsoft Graph, signed in as an app registration with the
    application permissions GroupMember.Read.All and User.ReadBasic.All (admin consent)."""

    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, cfg: dict, session=None):
        self.tenant = cfg["tenant_id"]
        self.client_id = cfg["client_id"]
        self.secret = _env(cfg.get("secret_env"), "the app registration's client secret")
        self.only = set(cfg.get("groups") or ())      # optional: just these group IDs
        self.http = _Http(session)

    def _pages(self, url, h, limit):
        items = []
        while url and len(items) < limit:
            d = self.http.get(url, h).json()
            items += d.get("value", [])
            url = d.get("@odata.nextLink")              # Graph's next page: a full URL
        return items[:limit]

    def groups(self) -> list[Group]:
        token = self.http.token(f"https://login.microsoftonline.com/{quote(self.tenant)}/oauth2/v2.0/token",
                                {"grant_type": "client_credentials", "client_id": self.client_id,
                                 "client_secret": self.secret, "scope": "https://graph.microsoft.com/.default"})
        h = {"Authorization": f"Bearer {token}"}
        raw = self._pages(f"{self.GRAPH}/groups?$select=id,displayName&$top={PAGE}", h, MAX_GROUPS)
        out = []
        for g in raw:
            if self.only and g["id"] not in self.only:
                continue
            users = self._pages(f"{self.GRAPH}/groups/{g['id']}/members/microsoft.graph.user"
                                f"?$select=id,displayName,userPrincipalName&$top={PAGE}", h, MAX_MEMBERS)
            out.append(Group(g["id"], g.get("displayName") or g["id"], tuple(
                Member(u["id"], u.get("userPrincipalName", ""), u.get("displayName") or u.get("userPrincipalName", ""))
                for u in users)))
        return out


class Okta:
    """Okta's management API with a read-only API token (Authorization: SSWS ...), e.g. from an
    admin account with the Read-only Administrator role."""

    def __init__(self, cfg: dict, session=None):
        self.org = cfg["org_url"].rstrip("/")
        self.token = _env(cfg.get("token_env"), "an Okta API token")
        self.http = _Http(session)

    def _pages(self, url, limit):
        items = []
        h = {"Authorization": f"SSWS {self.token}", "Accept": "application/json"}
        while url and len(items) < limit:
            r = self.http.get(url, h)
            items += r.json()
            url = r.links.get("next", {}).get("url")      # Okta pages with Link: <...>; rel="next"
        return items[:limit]

    def groups(self) -> list[Group]:
        out = []
        for g in self._pages(f"{self.org}/api/v1/groups?limit=200", MAX_GROUPS):
            users = self._pages(f"{self.org}/api/v1/groups/{g['id']}/users?limit=200", MAX_MEMBERS)
            name = (g.get("profile") or {}).get("name") or g["id"]
            out.append(Group(g["id"], name, tuple(
                Member(u["id"], (u.get("profile") or {}).get("login", ""),
                       " ".join(x for x in ((u.get("profile") or {}).get("firstName"),
                                            (u.get("profile") or {}).get("lastName")) if x)
                       or (u.get("profile") or {}).get("login", ""))
                for u in users if u.get("status") not in ("DEPROVISIONED", "SUSPENDED"))))
        return out


PROVIDERS = {"keycloak": Keycloak, "entra": Entra, "okta": Okta}


def connect(cfg: dict, session=None):
    try:
        return PROVIDERS[cfg["provider"]](cfg, session)
    except KeyError as e:
        raise DirectoryError(f"sign_in.directory is missing {e}") from None


# --- the access report ------------------------------------------------------------------------------

def access_report(groups: list[Group] | None, role_map: dict[str, str], role_titles: dict[str, str]) -> dict:
    """Who gets which role. A mapping key matches a group by name or by ID (Entra's tokens carry
    group IDs; Keycloak's and Okta's carry names). A person in several mapped groups gets the first
    in the map's order, exactly as sign-in decides (auth.sign_in.role_for)."""
    mapping = [{"group": k, "role": v, "role_title": role_titles.get(v, v)} for k, v in role_map.items()]
    if groups is None:
        return {"mapping": mapping, "people": None, "groups": None, "warnings": []}
    order = list(role_map)
    by_key = {}
    for g in groups:
        for key in (g.name, g.id):
            if key in role_map:
                by_key.setdefault(key, g)
    for m in mapping:
        g = by_key.get(m["group"])
        m["found"] = g is not None
        m["directory_name"] = g.name if g else None
        m["members"] = len(g.members) if g else 0

    people: dict[str, dict] = {}
    for key in order:
        g = by_key.get(key)
        for u in (g.members if g else ()):
            p = people.setdefault(u.id, {"username": u.username, "name": u.name, "groups": [], "role": None})
            p["groups"].append(g.name)
            if p["role"] is None:
                p["role"] = role_map[key]
    rows = sorted(people.values(), key=lambda p: (p["role"] or "~", p["name"].lower()))
    for p in rows:
        p["role_title"] = role_titles.get(p["role"], p["role"])
        p["also"] = [g for g in p["groups"][1:]]          # other mapped groups that did not decide the role

    warnings = [f"No group named or with ID '{m['group']}' in the directory: no one gets {m['role_title']} through it"
                for m in mapping if not m["found"]]
    warnings += [f"{p['name']} is in {', '.join(p['groups'])}: the role is {p['role_title']} (the first in sign_in.roles)"
                 for p in rows if p["also"]]
    unmapped = sorted(g.name for g in groups if g.name not in role_map and g.id not in role_map)
    return {"mapping": mapping, "people": rows, "unmapped_groups": unmapped, "warnings": warnings}


class CachedDirectory:
    """The directory's groups, asked at most once every `seconds` (the page refreshes often)."""

    def __init__(self, cfg: dict | None, seconds: int = 300, clock=time.monotonic):
        self.cfg, self.seconds, self.clock = cfg, seconds, clock
        self._at, self._groups, self._error = -1e18, None, None

    def groups(self) -> tuple[list[Group] | None, str | None]:
        if not self.cfg:
            return None, None
        if self.clock() - self._at > self.seconds:
            try:
                self._groups, self._error = connect(self.cfg).groups(), None
            except DirectoryError as e:
                self._groups, self._error = None, str(e)
            self._at = self.clock()
        return self._groups, self._error


def main(argv: list[str]) -> int:
    from profiles.loader import load_profile
    p = load_profile(argv[0] if argv else None)
    s = p.sign_in or {}
    if not s.get("roles"):
        print(f"Profile {p.name} has no sign_in.roles: nothing to report.")
        return 1
    titles = {k: r.title for k, r in p.roles.items()}
    groups, error = CachedDirectory(s.get("directory")).groups()
    report = access_report(groups, s["roles"], titles)
    print(f"Profile {p.name}: identity-provider group -> role")
    for m in report["mapping"]:
        found = "" if groups is None else ("" if m["found"] else "   NOT FOUND in the directory")
        print(f"  {m['group']:<40} -> {m['role_title']}{found}")
    if groups is None:
        print("\n" + (f"Directory not read: {error}" if error else "No sign_in.directory: people are not listed."))
        return 0 if not error else 2
    print(f"\nPeople ({len(report['people'])}):")
    for r in report["people"]:
        print(f"  {r['name']:<28} {r['username']:<32} {r['role_title']}")
    for w in report["warnings"]:
        print(f"  ! {w}")
    if report["unmapped_groups"]:
        print(f"\nGroups with no role here: {', '.join(report['unmapped_groups'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
