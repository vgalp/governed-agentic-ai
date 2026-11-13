"""SMART App Launch from inside the EHR (ADR 035).

The clinician opens the assistant from the patient's chart in Epic or Oracle Health. The EHR calls
the app's launch URL with its FHIR address (iss) and a launch code; the app sends the browser to the
EHR's sign-in with that code, and gets back a token that is the CLINICIAN's (user-level scopes), the
patient that chart is open on, and an ID token saying who the clinician is (fhirUser).

Compared with backend sign-in (the app reads as itself, every patient):
  - the EHR's own audit names the clinician for every read, and the EHR enforces what that clinician
    may see;
  - the patient comes from the open chart: no search by name;
  - the platform's role comes from who the EHR says the user is, not from a form.

What this module refuses (each a test):
  - a launch from an EHR that is not configured (`iss` allowlist): otherwise anyone could point the
    app at their own server and collect what users type;
  - a callback whose state is unknown, used twice, or more than 10 minutes old;
  - an ID token not signed by the EHR's published keys, for another app, expired, or with the
    wrong nonce;
  - a launch without a patient, or by a user type that has no role in the profile.
Tokens are kept server-side only, encrypted (fhir/sessions.py); the browser holds a random session ID.
"""

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

import jwt
import requests

from fhir.client import FhirError, _brief, _send
from fhir.sessions import LaunchSession, SessionStore

PENDING_SECONDS = 600            # from the launch to the callback
MAX_PENDING = 1000               # sign-ins in progress at once: a flood of launches cannot fill memory
ID_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384"]


@dataclass(frozen=True)
class LaunchSettings:
    issuers: tuple[str, ...]                 # the FHIR addresses (iss) this profile accepts launches from
    client_id: str
    redirect_uri: str
    scope: str
    roles: dict                              # fhirUser resource type -> platform role, e.g. {Practitioner: clinician}
    client_secret: str | None = None         # confidential client, symmetric (HTTP Basic)
    private_key_pem: bytes | None = None     # confidential client, asymmetric (private_key_jwt)
    key_id: str | None = None
    session_minutes: int = 480


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/")


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def user_type(fhir_user: str) -> str:
    """'Practitioner' from 'Practitioner/123' or 'https://ehr.example/fhir/Practitioner/123'."""
    parts = [p for p in urllib.parse.urlsplit(fhir_user).path.split("/") if p] or fhir_user.split("/")
    return parts[-2] if len(parts) >= 2 else ""


@dataclass
class _Pending:
    iss: str
    verifier: str
    nonce: str
    config: dict
    at: float


@dataclass
class Launcher:
    settings: LaunchSettings
    store: SessionStore
    http: object = None
    clock: object = time.time
    _pending: dict = field(default_factory=dict)
    _configs: dict = field(default_factory=dict)
    _keys: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.http = self.http or requests.Session()

    # --- discovery ---------------------------------------------------------------------------------

    def config(self, iss: str) -> dict:
        if iss not in self._configs:
            r = _send(self.http.get, f"{iss}/.well-known/smart-configuration",
                      headers={"Accept": "application/json"}, timeout=10)
            if r.status_code != 200:
                raise FhirError(f"the EHR's SMART configuration could not be read ({r.status_code})")
            c = r.json()
            for k in ("authorization_endpoint", "token_endpoint"):
                if not str(c.get(k, "")).startswith("https://") and not _local(c.get(k, "")):
                    raise FhirError(f"the EHR's SMART configuration has no https {k}")
            self._configs[iss] = c
        return self._configs[iss]

    # --- 1. the EHR opens the launch URL -------------------------------------------------------------

    def start(self, iss: str, launch: str) -> str:
        """Where to send the browser: the EHR's sign-in, carrying the launch code."""
        iss = _norm(iss)
        if iss not in {_norm(i) for i in self.settings.issuers}:
            raise FhirError("this EHR is not configured for this app (unknown iss)")
        if not launch or len(launch) > 2000:
            raise FhirError("the launch has no launch code")
        cfg = self.config(iss)
        verifier, challenge = pkce()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._pending = {s: p for s, p in self._pending.items() if now - p.at < PENDING_SECONDS}
            if len(self._pending) >= MAX_PENDING:
                raise FhirError("too many sign-ins in progress; try again in a few minutes")
            self._pending[state] = _Pending(iss, verifier, nonce, cfg, now)
        self.last_state = state             # the API binds it to the browser (a cookie) before redirecting
        q = {"response_type": "code", "client_id": self.settings.client_id, "redirect_uri": self.settings.redirect_uri,
             "launch": launch, "scope": self.settings.scope, "state": state, "aud": iss, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        ep = cfg["authorization_endpoint"]
        return ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(q)

    # --- 2. the EHR sends the browser back -------------------------------------------------------------

    def finish(self, code: str | None, state: str | None, error: str | None = None,
               browser_state: str | None = "") -> LaunchSession:
        """`browser_state`: the state the browser that started this sign-in holds (a cookie). It must
        match, so a sign-in started by someone else cannot be finished in this browser (login CSRF).
        None skips the check (only for callers that bind the state another way)."""
        with self._lock:
            pending = self._pending.pop(state or "", None)       # single use, whatever happens next
        if browser_state is not None and not secrets.compare_digest(str(browser_state or ""), str(state or "")):
            raise FhirError("this sign-in was not started in this browser; launch again from the chart")
        if error:
            raise FhirError(f"the EHR did not sign in: {error}")
        if pending is None or self.clock() - pending.at > PENDING_SECONDS:
            raise FhirError("this sign-in is unknown or has expired; launch again from the chart")
        if not code:
            raise FhirError("the EHR sent no code")
        token_ep = pending.config["token_endpoint"]
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.settings.redirect_uri,
                "code_verifier": pending.verifier}
        body = self._token_request(token_ep, form)
        if not body.get("patient"):
            raise FhirError("the EHR sent no patient: launch from a patient's chart (and grant launch/patient)")
        claims = self.verify_id_token(body.get("id_token"), pending)
        who = claims.get("fhirUser") or claims.get("profile")
        if not isinstance(who, str) or not who:
            raise FhirError("the EHR did not say who signed in (no fhirUser): ask for the openid fhirUser scopes")
        role = self.settings.roles.get(user_type(who))
        if not role:
            raise FhirError(f"a {user_type(who) or 'user'} has no role in this assistant")
        now = self.clock()
        session = LaunchSession(
            id=secrets.token_urlsafe(32), iss=pending.iss, patient=str(body["patient"]),
            encounter=body.get("encounter"), user=who, name=str(claims.get("name") or who)[:200], role=role,
            access_token=body["access_token"], refresh_token=body.get("refresh_token"),
            expires_at=now + float(body.get("expires_in", 300)), scope=body.get("scope", ""),
            token_endpoint=token_ep, created=now,
            ends_at=now + self.settings.session_minutes * 60)
        self.store.save(session)
        return session

    def _client_auth(self, token_ep: str, form: dict) -> dict:
        headers = {"Accept": "application/json"}
        s = self.settings
        if s.private_key_pem:
            now = int(self.clock())
            form.update(client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        client_assertion=jwt.encode(
                            {"iss": s.client_id, "sub": s.client_id, "aud": token_ep, "jti": str(uuid.uuid4()),
                             "iat": now, "exp": now + 240}, s.private_key_pem, algorithm="RS384",
                            headers={"typ": "JWT", **({"kid": s.key_id} if s.key_id else {})}))
        elif s.client_secret:
            pair = f"{s.client_id}:{s.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        else:
            form["client_id"] = s.client_id                        # public client: PKCE only
        return headers

    def _token_request(self, token_ep: str, form: dict) -> dict:
        headers = self._client_auth(token_ep, form)
        r = _send(self.http.post, token_ep, data=form, headers=headers, timeout=20)
        if r.status_code != 200:
            raise FhirError(f"the EHR refused the sign-in ({r.status_code}): {_brief(r)}")
        body = r.json()
        if not body.get("access_token"):
            raise FhirError("the EHR sent no access token")
        return body

    # --- who signed in ------------------------------------------------------------------------------------

    def verify_id_token(self, token: str | None, pending: _Pending) -> dict:
        if not token:
            raise FhirError("the EHR sent no ID token: ask for the openid fhirUser scopes")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise FhirError("the ID token is not a signed token") from None
        if header.get("alg") not in ID_ALGORITHMS:
            raise FhirError("the ID token's signing algorithm is not accepted")
        jwks_uri = pending.config.get("jwks_uri")
        if not jwks_uri:
            raise FhirError("the EHR publishes no keys (jwks_uri) to check who signed in")
        key = self._key(jwks_uri, header.get("kid"))
        issuer = pending.config.get("issuer")
        try:
            claims = jwt.decode(token, key, algorithms=[header["alg"]], audience=self.settings.client_id,
                                issuer=issuer if issuer else None, leeway=60,
                                options={"require": ["exp", "iat", "aud"], "verify_iss": bool(issuer)})
        except jwt.PyJWTError as e:
            raise FhirError(f"the ID token was not accepted ({type(e).__name__})") from None
        if claims.get("nonce") is not None and claims["nonce"] != pending.nonce:
            raise FhirError("the ID token is for another sign-in (nonce)")
        return claims

    def _key(self, jwks_uri: str, kid: str | None):
        keys = self._keys.get(jwks_uri)
        if keys is None or (kid and kid not in keys):              # first use, or the EHR rotated keys
            r = _send(self.http.get, jwks_uri, headers={"Accept": "application/json"}, timeout=10)
            if r.status_code != 200:
                raise FhirError(f"the EHR's keys could not be read ({r.status_code})")
            keys = {str(k.get("kid", "")): k for k in (r.json().get("keys") or []) if isinstance(k, dict)}
            self._keys[jwks_uri] = keys
        jwk = keys.get(kid or "") or (next(iter(keys.values())) if not kid and len(keys) == 1 else None)
        if jwk is None:
            raise FhirError("the ID token was signed with a key the EHR does not publish")
        return jwt.PyJWK(jwk).key

    # --- using the session ---------------------------------------------------------------------------------

    def session(self, sid: str | None) -> LaunchSession | None:
        """The live session, with a current access token (refreshed if it can be); None when over."""
        s = self.store.load(sid) if sid else None
        if s is None or self.clock() >= s.ends_at:
            return None
        if self.clock() < s.expires_at - 30:
            return s
        if not s.refresh_token:
            return None                                        # the EHR's token ended: launch again
        try:
            body = self._token_request(s.token_endpoint, {"grant_type": "refresh_token",
                                                          "refresh_token": s.refresh_token})
        except FhirError:
            return None
        s.access_token, s.expires_at = body["access_token"], self.clock() + float(body.get("expires_in", 300))
        s.refresh_token = body.get("refresh_token") or s.refresh_token
        self.store.save(s)
        return s

    def end(self, sid: str | None) -> None:
        if sid:
            self.store.delete(sid)


def settings_from(launch: dict, environ=None) -> LaunchSettings:
    """LaunchSettings from a profile's fhir.yaml `launch:` block and the environment or a vault."""
    import os

    from fhir.secrets import SecretError, get
    environ = environ if environ is not None else os.environ
    if not environ.get(launch["client_id_env"]):
        raise FhirError(f"set {launch['client_id_env']} in .env (the app's client ID for launches from the EHR)")

    def secret(name):
        ref = launch.get(f"{name}_ref") or (f"env:{launch[name + '_env']}" if launch.get(f"{name}_env") else None)
        try:
            return get(ref, environ) if ref else None
        except SecretError as e:
            raise FhirError(str(e)) from None
    pem = secret("private_key")
    return LaunchSettings(tuple(launch["issuers"]), environ[launch["client_id_env"]], launch["redirect_uri"],
                          launch["scope"], dict(launch["roles"]), secret("client_secret"),
                          pem.encode() if pem else None, launch.get("key_id"),
                          int(launch.get("session_minutes", 480)))


def launch_client(fhir_map, ref: str, profile: str, store: SessionStore | None = None, session=None):
    """For a record tool: a FHIR client acting as the clinician who launched from the chart. The FHIR
    address is the session's, which was checked against the profile's issuers at launch."""
    from fhir.client import FhirClient
    from fhir.sessions import default_store
    from fhir.transport import ServerPolicy
    store = store or default_store(profile)
    s = store.load_ref(ref)
    if s is None:
        raise FhirError("the session from the chart has ended: open the assistant from the chart again")
    if _norm(s.iss) not in {_norm(i) for i in (fhir_map.launch or {}).get("issuers", [])}:
        raise FhirError("this session is from an EHR the profile does not accept")
    return FhirClient(s.iss, LaunchAuth(store, ref=ref), session=session,
                      policy=ServerPolicy.from_settings(fhir_map.server))


def _local(url: str) -> bool:
    h = urllib.parse.urlsplit(url or "").hostname
    return (url or "").startswith("http://") and h in ("localhost", "127.0.0.1")


class LaunchAuth:
    """For FhirClient: the launched clinician's token, and the chart's patient (find_patient reads
    that patient and never searches). Built from the session ID (the API) or from its reference
    (the record tools, which never see the ID)."""

    from_chart = True

    def __init__(self, sessions: SessionStore, sid: str | None = None, clock=time.time, ref: str | None = None):
        self.sessions, self.sid, self.ref, self.clock = sessions, sid, ref, clock

    def _s(self) -> LaunchSession:
        s = self.sessions.load(self.sid) if self.sid else self.sessions.load_ref(self.ref or "")
        if s is None or self.clock() >= min(s.ends_at, s.expires_at):
            raise FhirError("the session from the chart has ended: open the assistant from the chart again")
        return s

    def token(self) -> str:
        return self._s().access_token

    @property
    def patient(self) -> str:
        return self._s().patient

    @property
    def scope_granted(self) -> str:
        return self._s().scope

    @property
    def cache_scope(self) -> str:
        """This clinician's session: cached answers are never served to another (fhir/cache.py)."""
        return "launch:" + (self.ref or self.sessions.ref(self.sid or ""))

    def invalidate(self) -> None:
        pass                       # a refused token cannot be renewed from here: the next call says so
