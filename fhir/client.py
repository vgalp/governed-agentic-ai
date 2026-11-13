"""A read-only FHIR R4 client, with SMART sign-in (ADR 013, ADR 032).

Read-only by construction: the only request this client makes to the FHIR server is an HTTP GET
(`read`, `search`, `capability`). There is no code path that creates, updates or deletes. The one
POST is to the authorization server's token endpoint, to exchange a signed assertion for a
short-lived access token.

SMART Backend Services (used by Epic and others): the app proves who it is with a JSON Web Token
signed by its private key (RS384), and gets an access token valid for a few minutes. The private
key is read from a file named in the environment and never leaves this machine; the matching public
key is published as a JWK Set, where the authorization server fetches it. Some servers (Oracle Health
system accounts) also accept a client secret instead of a signed assertion.

The sign-in is set by the mapping's `server.auth`:
  none           an open server (Oracle Health's open sandbox); no token
  smart_backend  the app signs in as itself (Epic, Oracle Health system accounts)
  smart_user     a person signed in once in a browser (MEDITECH Greenfield): fhir/smart_user.py
"""

import base64
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import jwt
import requests

from fhir.errors import FhirDeadlineError, FhirError, FhirResponseError, FhirUnavailableError
from fhir.metrics import resource_label
from fhir.resilience import bounded_timeout, current_deadline
from fhir.transport import FhirTimeoutError, FhirUnreachableError, ServerPolicy, Transport, shared_transport
from fhir.validation import body_of, check_capability, check_page, check_resource

FHIR_JSON = "application/fhir+json"
log = logging.getLogger(__name__)

__all__ = ["FhirClient", "FhirDeadlineError", "FhirError", "FhirResponseError", "FhirUnavailableError",
           "SmartBackendAuth", "SmartBackendSettings", "client_from_settings", "retry_after"]


@dataclass(frozen=True)
class SmartBackendSettings:
    token_url: str
    client_id: str
    private_key_pem: bytes
    key_id: str | None
    scope: str | None = None
    client_secret: str | None = None     # instead of a key: HTTP Basic (client_secret_basic)


class SmartBackendAuth:
    """Gets and caches access tokens with a signed client assertion (client_credentials grant)."""

    ASSERTION_SECONDS = 240           # Epic accepts at most 5 minutes

    def __init__(self, s: SmartBackendSettings, session=None, clock=time.time):
        self.s, self.session, self.clock = s, session or requests.Session(), clock
        self._token, self._expires = None, 0.0
        self._lock = threading.Lock()             # the chart brief reads several record types at once

    def assertion(self) -> str:
        now = int(self.clock())
        claims = {"iss": self.s.client_id, "sub": self.s.client_id, "aud": self.s.token_url,
                  "jti": str(uuid.uuid4()), "iat": now, "nbf": now, "exp": now + self.ASSERTION_SECONDS}
        head = {"typ": "JWT", **({"kid": self.s.key_id} if self.s.key_id else {})}
        return jwt.encode(claims, self.s.private_key_pem, algorithm="RS384", headers=head)

    def invalidate(self) -> None:
        """The server refused the token (revoked, or ended early): ask for a new one next time."""
        with self._lock:
            self._token, self._expires = None, 0.0

    def token(self) -> str:
        with self._lock:                          # one sign-in at a time; the others reuse its token
            return self._token_locked()

    def _token_locked(self) -> str:
        if self._token and self.clock() < self._expires - 30:
            return self._token
        form, headers = {"grant_type": "client_credentials"}, {"Accept": "application/json"}
        if self.s.private_key_pem:
            form.update({"client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                         "client_assertion": self.assertion()})
        else:
            pair = f"{self.s.client_id}:{self.s.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        if self.s.scope:
            form["scope"] = self.s.scope
        r = _send(self.session.post, self.s.token_url, data=form, headers=headers, timeout=20)
        if r.status_code != 200:
            raise FhirError(f"token request refused ({r.status_code}): {_brief(r)}")
        body = r.json()
        self._token = body["access_token"]
        self._expires = self.clock() + float(body.get("expires_in", 300))
        self.scope_granted = body.get("scope")
        return self._token


BUSY = {429, 502, 503, 504}       # the server asks us to wait, or a gateway in front of it failed
MAX_WAIT = 10.0                   # seconds: a longer Retry-After is not waited for (the question would hang)


def retry_after(r, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After (seconds form), else 1, 2, 4 …"""
    try:
        return min(MAX_WAIT, max(0.0, float(r.headers.get("Retry-After"))))
    except (TypeError, ValueError, AttributeError):
        return min(MAX_WAIT, float(2 ** attempt))


def _has_retry_after(r) -> bool:
    return bool((getattr(r, "headers", None) or {}).get("Retry-After"))


def equal_jitter(seconds: float) -> float:
    """Half the wait fixed, half random: clients that failed together do not retry together."""
    return seconds / 2 + random.uniform(0, seconds / 2)


class FhirClient:
    """GET-only access to one FHIR R4 server.

    Requests go through the server's shared `Transport` (fhir/transport.py: pooling, concurrency and
    rate limits, circuit breaker, cache, metrics). This class adds what needs the sign-in or the
    FHIR semantics: the token, signing in again once on 401, waiting and trying again when the
    server is busy, following search pages, and checking every answer (fhir/validation.py).
    """

    PAGE = 50                         # resources asked for per page; servers cap _count anyway

    def __init__(self, base_url: str, auth=None, session=None, timeout: float = 20, max_pages: int = 6,
                 sleep: Callable[[float], None] = time.sleep, *, policy: ServerPolicy | None = None,
                 transport: Transport | None = None, jitter: Callable[[float], float] | None = equal_jitter):
        self.base = base_url.rstrip("/")
        self.auth, self.max_pages, self.sleep, self.jitter = auth, max_pages, sleep, jitter
        policy = policy or ServerPolicy(timeout=float(timeout))
        if transport is None:
            # A session handed in (a test double, a caller's own) gets a transport of its own;
            # otherwise every client of this server in the process shares one.
            transport = (Transport(self.base, policy, session=session, sleep=sleep) if session is not None
                         else shared_transport(self.base, policy))
        self.transport = transport
        self.session = transport.session

    @property
    def timeout(self) -> float:
        return self.transport.policy.timeout

    @property
    def RETRIES(self) -> int:         # noqa: N802 (kept from the earlier class constant)
        return self.transport.policy.retries

    # --- reading --------------------------------------------------------------------------------------

    def capability(self) -> dict:
        return check_capability(self._get(f"{self.base}/metadata", cache=False))

    def read(self, resource_type: str, rid: str) -> dict:
        body = self._get(f"{self.base}/{resource_type}/{urllib.parse.quote(str(rid), safe='')}")
        return self._valid(check_resource, body, resource_type, resource_type, rid)

    def search(self, resource_type: str, params: dict, limit: int = 50) -> list[dict]:
        """Resources of the type matching the search, following 'next' links up to max_pages."""
        return self.search_all(resource_type, params, limit)[0]

    def search_all(self, resource_type: str, params: dict, limit: int = 50) -> tuple[list[dict], bool]:
        """(resources, more): up to `limit` resources, following 'next' links up to max_pages, and
        whether the server holds more than were read, or said it could not return everything. A
        list cut off at a cap must say so: a partial allergy list shown as the whole one is worse
        than none."""
        out: list[dict] = []
        url, q = f"{self.base}/{resource_type}", dict(params)
        if "_count" in q:
            q["_count"] = min(int(q["_count"]), self.PAGE)
        total, nxt, partial = None, None, False
        for _ in range(self.max_pages):
            page = self._valid(check_page, self._get(url, q), resource_type, resource_type)
            if total is None:
                total = page.total
            if page.problems:
                partial = True
                log.warning("%s search on %s returned errors: %s", resource_type, self.transport.server,
                            "; ".join(page.problems)[:300])
            out.extend(page.matches)
            nxt = page.next_url
            if not nxt or len(out) >= limit:
                break
            url, q = nxt, None
        more = partial or len(out) > limit or nxt is not None or (total is not None and total > min(len(out), limit))
        return out[:limit], more

    # --- one GET, with sign-in, waiting and trying again ---------------------------------------------

    def _inside_base(self, url: str) -> bool:
        """The URL is on the configured server: same scheme and host, path under the base path.
        (A prefix check on the string let https://fhir.epic.com.example.org through.)"""
        u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base)
        return (u.scheme, (u.hostname or "").lower(), u.port) == (b.scheme, (b.hostname or "").lower(), b.port) \
            and (u.path == b.path or u.path.startswith(b.path.rstrip("/") + "/"))

    def _get(self, url: str, params: dict | None = None, cache: bool = True) -> dict:
        if not self._inside_base(url):
            raise FhirError(f"refusing to follow a link outside {self.base}")
        resource = resource_label(self.base, url)
        store = self.transport.cache if cache else None
        scope = getattr(self.auth, "cache_scope", None) if store is not None else None
        if scope and (hit := store.get(scope, url, params)) is not None:
            return hit

        signed_again, attempt = False, 0
        while True:
            headers = {"Accept": FHIR_JSON}
            if self.auth:
                headers["Authorization"] = f"Bearer {self.auth.token()}"
            try:
                r = self.transport.send(url, params=params, headers=headers, resource=resource)
            except (FhirTimeoutError, FhirUnreachableError) as e:
                # A GET changes nothing: trying it again is safe. No wait; the breaker handles an outage.
                if attempt < self.RETRIES and self._wait(0.0, "timeout" if isinstance(e, FhirTimeoutError)
                                                         else "unreachable"):
                    attempt += 1
                    continue
                if isinstance(e, FhirTimeoutError):
                    raise FhirTimeoutError(f"{e}; a slow server can be given longer with server.timeout "
                                           "in fhir.yaml") from None
                raise
            if r.status_code == 401 and self.auth and hasattr(self.auth, "invalidate") and not signed_again:
                self.auth.invalidate()             # a token the server no longer takes: sign in again, once
                if scope:
                    store.drop_scope(scope)
                signed_again = True
                self.transport.metrics.inc("fhir_retries_total", server=self.transport.server, reason="401")
                continue
            if r.status_code in BUSY and attempt < self.RETRIES:
                wait = retry_after(r, attempt)
                if self.jitter is not None and not _has_retry_after(r):
                    wait = self.jitter(wait)
                if self._wait(wait, str(r.status_code)):
                    attempt += 1
                    continue
            break
        if r.status_code != 200:
            # the path only: a paged 'next' link carries the search (patient ids) in its query string
            path = urllib.parse.urlsplit(url).path.removeprefix(urllib.parse.urlsplit(self.base).path)
            raise FhirError(f"GET {path or '/'} failed ({r.status_code}): {_brief(r)}")
        body = self._valid(body_of, r, resource)
        if scope:
            store.put(scope, url, params, body)
        return body

    def _wait(self, seconds: float, reason: str) -> bool:
        """Wait before trying again, unless that would overrun the call's budget (then: don't)."""
        d = current_deadline()
        if d is not None and seconds >= d.remaining():
            return False
        self.transport.metrics.inc("fhir_retries_total", server=self.transport.server, reason=reason)
        if seconds > 0:
            self.sleep(seconds)
        return True

    def _valid(self, check, value, resource: str, *args):
        """Run a validation check; count a failure in the metrics before raising it."""
        try:
            return check(value, *args)
        except FhirResponseError:
            self.transport.metrics.inc("fhir_response_invalid_total", server=self.transport.server,
                                       resource=resource)
            raise


def _send(method, url: str, retry: int = 0, **kw):
    """One request to an authorization server (token, JWKS, discovery); a server that cannot be
    reached is a FhirError like a refusal, not a crash. A request that timed out is tried again
    `retry` times. The timeout is cut to what is left of the call's budget."""
    if "timeout" in kw:
        kw["timeout"] = bounded_timeout(float(kw["timeout"]))
        if kw["timeout"] <= 0:
            raise FhirDeadlineError("the time for this question ran out while signing in")
    for attempt in range(retry + 1):
        try:
            return method(url, **kw)
        except requests.Timeout as e:
            if attempt < retry:
                continue
            last = e
        except requests.RequestException as e:
            last = e
            break
    host = urllib.parse.urlsplit(url).hostname
    raise FhirError(f"could not reach {host} ({type(last).__name__})") from None


def _brief(r) -> str:
    why = (getattr(r, "headers", None) or {}).get("WWW-Authenticate", "")
    m = re.search(r'error="([^"]+)"', why)
    if m:                                      # e.g. insufficient_scope: the app lacks that API
        return m.group(1) + (" (the app is not allowed this request: check the APIs or scopes it was granted)"
                             if m.group(1) == "insufficient_scope" else "")
    try:
        body = r.json()
    except ValueError:
        return (r.text or "")[:200]
    if body.get("resourceType") == "OperationOutcome":
        return "; ".join(i.get("diagnostics") or (i.get("details") or {}).get("text", "") or i.get("code", "")
                         for i in body.get("issue", []))[:300]
    return str({k: body[k] for k in ("error", "error_description") if k in body})[:300]


def _secret(ref: str, environ) -> str:
    from fhir.secrets import SecretError, get
    try:
        return get(ref, environ)
    except SecretError as e:
        raise FhirError(str(e)) from None


def _need(server: dict, environ, keys: tuple[str, ...], docs: str) -> None:
    missing = [server[k] for k in keys if server.get(k) and not environ.get(server[k])]
    if missing:
        raise FhirError(f"set {' and '.join(missing)} in .env (see {docs})")


def client_from_settings(server: dict, environ=os.environ, session=None) -> FhirClient:
    """The client for a mapping's `server:` block. Secrets come from the environment only."""
    auth, kind = None, server.get("auth", "none")
    docs = server.get("docs", "docs/epic/README.md")
    if kind == "smart_backend":
        _need(server, environ, ("client_id_env", "private_key_env", "client_secret_env"), docs)
        pem, secret = b"", None
        if server.get("private_key_ref"):                 # a vault, or a file (fhir/secrets.py)
            pem = _secret(server["private_key_ref"], environ).encode()
        elif server.get("private_key_env"):
            key = Path(environ[server["private_key_env"]]).expanduser()
            if not key.exists():
                raise FhirError(f"private key not found: {key}")
            pem = key.read_bytes()
        elif server.get("client_secret_ref"):
            secret = _secret(server["client_secret_ref"], environ)
        else:
            secret = environ[server["client_secret_env"]]
        auth = SmartBackendAuth(SmartBackendSettings(server["token_url"], environ[server["client_id_env"]], pem,
                                                     server.get("key_id"), server.get("scope"), secret),
                                session=session)
    elif kind == "smart_user":
        from fhir.smart_user import SmartUserAuth, SmartUserSettings
        _need(server, environ, ("client_id_env", "client_secret_env"), docs)
        settings = SmartUserSettings(
            server["token_url"], environ[server["client_id_env"]],
            _secret(server["client_secret_ref"], environ) if server.get("client_secret_ref") else
            environ.get(server["client_secret_env"]) if server.get("client_secret_env") else None,
            server["token_store"], **{k: server[k] for k in ("redirect_uri", "scope", "authorize_url") if server.get(k)})
        auth = SmartUserAuth(settings, session=session, connect_hint=server.get("connect_hint", ""))
    return FhirClient(server["base_url"], auth, session=session, policy=ServerPolicy.from_settings(server))
