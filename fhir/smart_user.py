"""SMART sign-in by a person (authorization code with PKCE), for FHIR servers that do not offer
backend (system) access to outside apps: MEDITECH Greenfield is one (ADR 032).

The person signs in once in a browser (scripts/fhir_connect.py). The server sends back a short-lived
access token and a refresh token; both are kept in a token file outside the repository
(~/.config/governed-agentic-ai/<name>/token.json, readable by the owner only). From then on the client
refreshes the access token itself, with no browser, until the refresh token expires or is revoked.

What the person may see is what the app may read: a patient-facing sign-in gives a token for one
patient (the server names it in the token response), so the assistant can only read that patient.

Read-only like the rest of fhir/: the POSTs here go to the authorization server's token endpoint,
never to the FHIR server.
"""

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests

from fhir.client import FhirError, _brief, _send

try:                                   # file locking: macOS and Linux; on Windows, refreshes are not locked
    import fcntl
except ImportError:                    # pragma: no cover
    fcntl = None


# --- PKCE and the sign-in link ------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge): RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(authorize_endpoint: str, client_id: str, redirect_uri: str, scope: str, aud: str,
                  state: str, challenge: str) -> str:
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scope,
         "state": state, "aud": aud, "code_challenge": challenge, "code_challenge_method": "S256"}
    return authorize_endpoint + ("&" if "?" in authorize_endpoint else "?") + urllib.parse.urlencode(q)


def smart_configuration(base_url: str, session=None, timeout: float = 20) -> dict:
    """The server's SMART configuration (its sign-in and token endpoints, what it supports)."""
    url = base_url.rstrip("/") + "/.well-known/smart-configuration"
    r = _send((session or requests.Session()).get, url, headers={"Accept": "application/json"}, timeout=timeout)
    if r.status_code != 200:
        raise FhirError(f"GET /.well-known/smart-configuration failed ({r.status_code}): {_brief(r)}")
    return r.json()


# --- the token file -------------------------------------------------------------------------------------

class TokenStore:
    """The tokens of one sign-in, in a file only the owner can read. Never in the repository."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text())
        except ValueError as e:
            raise FhirError(f"token file is not readable JSON: {self.path} ({e})") from None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)                 # whole file or nothing
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def locked(self):
        """One refresh at a time: several tool servers share the file, and a server may issue a new
        refresh token with each refresh (the old one then stops working)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _keep(body: dict, now: float, old: dict | None = None) -> dict:
    """What is kept from a token response: the tokens, when the access token ends, the scope and the
    patient the token is for. The refresh token stays if the server did not send a new one."""
    old = old or {}
    return {"access_token": body["access_token"],
            "expires_at": now + float(body.get("expires_in", 300)),
            "refresh_token": body.get("refresh_token") or old.get("refresh_token"),
            "scope": body.get("scope", old.get("scope")),
            "patient": body.get("patient", old.get("patient")),
            "obtained_at": old.get("obtained_at", now)}


# --- sign-in and refresh ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class SmartUserSettings:
    token_url: str
    client_id: str
    client_secret: str | None          # confidential apps (MEDITECH: sent in the form body)
    token_store: str
    redirect_uri: str = "http://localhost:8765/callback"
    scope: str = "openid fhirUser launch/patient offline_access patient/*.read"
    authorize_url: str | None = None   # None: read from the server's SMART configuration


def _client_form(s: SmartUserSettings) -> dict:
    form = {"client_id": s.client_id}
    if s.client_secret:
        form["client_secret"] = s.client_secret
    return form


def exchange_code(s: SmartUserSettings, code: str, verifier: str, session=None, clock=time.time) -> dict:
    """Trade the code from the browser for tokens, and keep them. The code is single-use and short-lived
    (MEDITECH: 60 seconds), so this runs as soon as the browser returns."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri,
            "code_verifier": verifier, **_client_form(s)}
    r = _send((session or requests.Session()).post, s.token_url, data=form, headers={"Accept": "application/json"},
              timeout=20)
    if r.status_code != 200:
        raise FhirError(f"sign-in refused ({r.status_code}): {_brief(r)}")
    kept = _keep(r.json(), clock())
    TokenStore(s.token_store).save(kept)
    return kept


class SmartUserAuth:
    """Access tokens from the token file, refreshed when they are about to end."""

    def __init__(self, s: SmartUserSettings, session=None, clock=time.time, connect_hint: str = ""):
        self.s, self.session, self.clock = s, session or requests.Session(), clock
        self.store = TokenStore(s.token_store)
        self.hint = connect_hint or "run scripts/fhir_connect.py to sign in"

    def _current(self) -> dict:
        data = self.store.load()
        if not data or not data.get("access_token"):
            raise FhirError(f"not signed in to the FHIR server: {self.hint}")
        return data

    def invalidate(self) -> None:
        """The server refused the access token: refresh it on the next request."""
        with self.store.locked():
            data = self.store.load()
            if data:
                self.store.save({**data, "expires_at": 0})

    @property
    def patient(self) -> str | None:
        """The patient the sign-in is for, when the server named one."""
        return (self.store.load() or {}).get("patient")

    @property
    def scope_granted(self) -> str | None:
        return (self.store.load() or {}).get("scope")

    @property
    def cache_scope(self) -> str:
        """The person who signed in (one token file per sign-in): see fhir/cache.py."""
        return "user:" + hashlib.sha256(str(self.s.token_store).encode()).hexdigest()[:24]

    def token(self) -> str:
        data = self._current()
        if self.clock() < data.get("expires_at", 0) - 30:
            return data["access_token"]
        with self.store.locked():
            data = self._current()                     # another process may have refreshed meanwhile
            if self.clock() < data.get("expires_at", 0) - 30:
                return data["access_token"]
            if not data.get("refresh_token"):
                raise FhirError(f"the sign-in has ended and there is no refresh token: {self.hint}")
            form = {"grant_type": "refresh_token", "refresh_token": data["refresh_token"], **_client_form(self.s)}
            r = _send(self.session.post, self.s.token_url, data=form, headers={"Accept": "application/json"},
                      timeout=20)
            if r.status_code != 200:
                raise FhirError(f"token refresh refused ({r.status_code}): {_brief(r)}; {self.hint}")
            kept = _keep(r.json(), self.clock(), data)
            self.store.save(kept)
            return kept["access_token"]
