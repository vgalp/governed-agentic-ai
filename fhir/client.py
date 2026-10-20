"""A read-only FHIR R4 client, with SMART Backend Services sign-in (ADR 013).

Read-only by construction: the only request this client makes to the FHIR server is an HTTP GET
(`read`, `search`, `capability`). There is no code path that creates, updates or deletes. The one
POST is to the authorization server's token endpoint, to exchange a signed assertion for a
short-lived access token.

SMART Backend Services (used by Epic and others): the app proves who it is with a JSON Web Token
signed by its private key (RS384), and gets an access token valid for a few minutes. The private
key is read from a file named in the environment and never leaves this machine; the matching public
key is published as a JWK Set, where the authorization server fetches it.
"""

import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import jwt
import requests

FHIR_JSON = "application/fhir+json"


class FhirError(Exception):
    """The FHIR or authorization server refused or failed a request."""


@dataclass(frozen=True)
class SmartBackendSettings:
    token_url: str
    client_id: str
    private_key_pem: bytes
    key_id: str
    scope: str | None = None


class SmartBackendAuth:
    """Gets and caches access tokens with a signed client assertion (client_credentials grant)."""

    ASSERTION_SECONDS = 240           # Epic accepts at most 5 minutes

    def __init__(self, s: SmartBackendSettings, session=None, clock=time.time):
        self.s, self.session, self.clock = s, session or requests.Session(), clock
        self._token, self._expires = None, 0.0

    def assertion(self) -> str:
        now = int(self.clock())
        claims = {"iss": self.s.client_id, "sub": self.s.client_id, "aud": self.s.token_url,
                  "jti": str(uuid.uuid4()), "iat": now, "nbf": now, "exp": now + self.ASSERTION_SECONDS}
        return jwt.encode(claims, self.s.private_key_pem, algorithm="RS384",
                          headers={"kid": self.s.key_id, "typ": "JWT"})

    def token(self) -> str:
        if self._token and self.clock() < self._expires - 30:
            return self._token
        form = {"grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self.assertion()}
        if self.s.scope:
            form["scope"] = self.s.scope
        r = self.session.post(self.s.token_url, data=form, headers={"Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            raise FhirError(f"token request refused ({r.status_code}): {_brief(r)}")
        body = r.json()
        self._token = body["access_token"]
        self._expires = self.clock() + float(body.get("expires_in", 300))
        self.scope_granted = body.get("scope")
        return self._token


class FhirClient:
    """GET-only access to one FHIR R4 server."""

    def __init__(self, base_url: str, auth: SmartBackendAuth | None = None, session=None,
                 timeout: float = 20, max_pages: int = 5):
        self.base = base_url.rstrip("/")
        self.auth, self.session = auth, session or requests.Session()
        self.timeout, self.max_pages = timeout, max_pages

    def _get(self, url: str, params: dict | None = None) -> dict:
        if not url.startswith(self.base):
            raise FhirError(f"refusing to follow a link outside {self.base}")
        headers = {"Accept": FHIR_JSON}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth.token()}"
        r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise FhirError(f"GET {url.removeprefix(self.base)} failed ({r.status_code}): {_brief(r)}")
        return r.json()

    def capability(self) -> dict:
        return self._get(f"{self.base}/metadata")

    def read(self, resource_type: str, rid: str) -> dict:
        return self._get(f"{self.base}/{resource_type}/{rid}")

    def search(self, resource_type: str, params: dict, limit: int = 50) -> list[dict]:
        """Resources of the type matching the search, following 'next' links up to max_pages."""
        out, url, q = [], f"{self.base}/{resource_type}", dict(params)
        for _ in range(self.max_pages):
            bundle = self._get(url, q)
            for e in bundle.get("entry") or []:
                res = e.get("resource") or {}
                if res.get("resourceType") == resource_type:
                    out.append(res)
            nxt = next((link["url"] for link in bundle.get("link") or [] if link.get("relation") == "next"), None)
            if not nxt or len(out) >= limit:
                break
            url, q = nxt, None
        return out[:limit]


def _brief(r) -> str:
    why = (getattr(r, "headers", None) or {}).get("WWW-Authenticate", "")
    m = re.search(r'error="([^"]+)"', why)
    if m:                                      # e.g. insufficient_scope: the app lacks that API
        return m.group(1) + (" (the app is not allowed this request: check its APIs at fhir.epic.com)"
                             if m.group(1) == "insufficient_scope" else "")
    try:
        body = r.json()
    except ValueError:
        return (r.text or "")[:200]
    if body.get("resourceType") == "OperationOutcome":
        return "; ".join(i.get("diagnostics") or (i.get("details") or {}).get("text", "") or i.get("code", "")
                         for i in body.get("issue", []))[:300]
    return str({k: body[k] for k in ("error", "error_description") if k in body})[:300]


def client_from_settings(server: dict, environ=os.environ, session=None) -> FhirClient:
    """The client for a mapping's `server:` block. Secrets come from the environment only."""
    auth = None
    if server.get("auth") == "smart_backend":
        missing = [server[k] for k in ("client_id_env", "private_key_env") if not environ.get(server[k])]
        if missing:
            raise FhirError(f"set {' and '.join(missing)} in .env (see docs/epic/README.md)")
        key = Path(environ[server["private_key_env"]]).expanduser()
        if not key.exists():
            raise FhirError(f"private key not found: {key}")
        auth = SmartBackendAuth(SmartBackendSettings(server["token_url"], environ[server["client_id_env"]],
                                                     key.read_bytes(), server["key_id"], server.get("scope")),
                                session=session)
    return FhirClient(server["base_url"], auth, session=session)
