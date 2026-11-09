"""Sign-in for the chat API (ADR 028): who is asking, and in which role, comes from the
organization's own identity provider (OpenID Connect), never from the request body.

Two modes, set by the profile's `sign_in.mode` (profile.yaml) and overridden by the environment
variable GOVERNED_SIGN_IN:

  demo   The caller says who they are and which role they have (user_id and role in the request).
         For a demonstration on one machine only: anyone can claim any role.
  oidc   Every request carries an access token from the identity provider (Keycloak, Entra ID,
         Okta, Google ...): `Authorization: Bearer <token>`. The token's signature, issuer, audience
         and expiry are checked; the person comes from one claim, the role from the groups claim,
         mapped to this profile's roles by `sign_in.roles`. Anything else in the request is ignored.

Fails closed: no token, a token that does not verify, an identity provider that cannot be reached,
or a person in no mapped group gives no access (no role: the pipeline refuses role-checked tools).

The token itself is never stored or logged.
"""

import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import jwt
import requests

DEMO, OIDC = "demo", "oidc"
ENV_MODE = "GOVERNED_SIGN_IN"
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SignInRequired(Exception):
    """No valid sign-in: the API answers 401."""


class SignInError(Exception):
    """The sign-in settings cannot work (raised at startup, not per request)."""


@dataclass(frozen=True)
class Caller:
    user_id: str
    name: str
    role: str | None
    groups: tuple[str, ...] = ()


def get_json(url: str) -> dict:
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not answer a JSON object")
    return data


def _check_issuer_url(issuer: str) -> str:
    u = urlparse(issuer)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in LOCAL_HOSTS):
        return issuer.rstrip("/")
    raise SignInError(f"sign_in.issuer must be https (http only for a provider on this machine): {issuer}")


class OidcVerifier:
    """Checks access tokens against one issuer: signature (keys from the issuer's JWKS), issuer,
    audience, expiry. The discovery document and the keys are cached; an unknown key ID fetches the
    keys again (the provider rotated them), at most once a minute."""

    def __init__(self, issuer: str, audience: str, fetch_json=get_json, clock=time.time, leeway: int = 30,
                 keys_ttl: int = 3600):
        self.issuer, self.audience = _check_issuer_url(issuer), audience
        self._fetch, self._clock, self.leeway, self.keys_ttl = fetch_json, clock, leeway, keys_ttl
        self._discovery: dict | None = None
        self._keys: dict[str, dict] = {}
        self._keys_at = -1e18
        self._lock = threading.Lock()

    def discovery(self) -> dict:
        if self._discovery is None:
            d = self._fetch(f"{self.issuer}/.well-known/openid-configuration")
            if str(d.get("issuer", "")).rstrip("/") != self.issuer:
                raise SignInRequired("the identity provider names a different issuer")
            if not d.get("jwks_uri"):
                raise SignInRequired("the identity provider publishes no signing keys")
            self._discovery = d
        return self._discovery

    def _load_keys(self) -> None:
        jwks = self._fetch(self.discovery()["jwks_uri"])
        keys = {}
        for k in jwks.get("keys") or []:
            if isinstance(k, dict) and k.get("use", "sig") == "sig":
                keys[str(k.get("kid", ""))] = k
        self._keys, self._keys_at = keys, self._clock()

    def _key(self, kid: str | None):
        with self._lock:
            now = self._clock()
            if now - self._keys_at > self.keys_ttl:
                self._load_keys()
            jwk = self._keys.get(kid or "")
            if jwk is None and kid is None and len(self._keys) == 1:
                jwk = next(iter(self._keys.values()))
            if jwk is None and now - self._keys_at > 60:          # rotated keys
                self._load_keys()
                jwk = self._keys.get(kid or "")
        if jwk is None:
            raise SignInRequired("the token was signed with an unknown key")
        return jwt.PyJWK(jwk).key

    def verify(self, token: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise SignInRequired("the token is not a signed token") from None
        alg = header.get("alg")
        if alg not in ALGORITHMS:                     # never "none", never a shared-secret algorithm
            raise SignInRequired("the token's signing algorithm is not accepted")
        try:
            key = self._key(header.get("kid"))
            claims = jwt.decode(token, key, algorithms=[alg], issuer=self.issuer, leeway=self.leeway,
                                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False})
        except SignInRequired:
            raise
        except jwt.ExpiredSignatureError:
            raise SignInRequired("the token has expired") from None
        except (jwt.PyJWTError, requests.RequestException, ValueError, KeyError) as e:
            raise SignInRequired(f"the token could not be verified ({type(e).__name__})") from None
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else list(aud or [])
        if self.audience not in auds and claims.get("azp") != self.audience:
            raise SignInRequired("the token was not issued for this service")
        return claims


def _groups(claims: dict, claim: str) -> tuple[str, ...]:
    """The person's groups: a list of names (Keycloak may send full paths, "/nurses")."""
    value = claims
    for part in claim.split("."):                  # e.g. "realm_access.roles"
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(str(g).lstrip("/") for g in value if isinstance(g, str | int))


def role_for(groups: tuple[str, ...], role_map: dict[str, str]) -> str | None:
    """The profile role for these groups: the first entry of `sign_in.roles` whose group the
    person is in. The administrator orders the map; no mapped group means no role."""
    held = set(groups)
    return next((role for group, role in role_map.items() if group in held), None)


class SignIn:
    """The sign-in for one profile. `caller()` answers who is asking, or raises SignInRequired."""

    def __init__(self, settings: dict | None, env=None, fetch_json=get_json, clock=time.time):
        s = dict(settings or {})
        env = os.environ if env is None else env
        mode = (env.get(ENV_MODE) or s.get("mode") or DEMO).strip().lower()
        if mode not in (DEMO, OIDC):
            raise SignInError(f"{ENV_MODE} must be {DEMO} or {OIDC}, not {mode!r}")
        self.mode = mode
        self.settings = s
        self.roles: dict[str, str] = dict(s.get("roles") or {})
        self.user_claim = s.get("user_claim") or "preferred_username"
        self.groups_claim = s.get("groups_claim") or "groups"
        self.dashboard_roles = tuple(s.get("dashboard_roles") or ())
        self.admin_roles = tuple(s.get("admin_roles") or ())
        self.verifier = None
        if mode == OIDC:
            missing = [k for k in ("issuer", "audience", "client_id") if not s.get(k)]
            if missing:
                raise SignInError(f"sign-in is oidc but the profile's sign_in has no {', '.join(missing)}")
            if not self.roles:
                raise SignInError("sign-in is oidc but sign_in.roles maps no group to a role")
            self.verifier = OidcVerifier(s["issuer"], s["audience"], fetch_json, clock,
                                         int(s.get("leeway_seconds", 30)))

    @classmethod
    def for_profile(cls, profile, env=None) -> "SignIn":
        return cls(getattr(profile, "sign_in", None), env)

    @property
    def on(self) -> bool:
        return self.mode == OIDC

    def public_config(self) -> dict:
        """What a browser page needs to sign a person in (no secrets: a public client with PKCE)."""
        if not self.on:
            return {"mode": DEMO}
        return {"mode": OIDC, "issuer": self.verifier.issuer, "client_id": self.settings["client_id"],
                "scope": self.settings.get("scope") or "openid profile"}

    def caller(self, authorization: str | None, demo_user: str | None = None,
               demo_role: str | None = None) -> Caller:
        if not self.on:
            user = (demo_user or "demo-user")[:200]
            return Caller(user, user, demo_role or None)
        token = bearer(authorization)
        if not token:
            raise SignInRequired("sign in first: this service needs an access token")
        claims = self.verifier.verify(token)
        user = claims.get(self.user_claim) or claims.get("sub")
        if not isinstance(user, str) or not user.strip():
            raise SignInRequired(f"the token has no {self.user_claim}")
        groups = _groups(claims, self.groups_claim)
        name = claims.get("name") if isinstance(claims.get("name"), str) else user
        return Caller(user.strip()[:200], name[:200], role_for(groups, self.roles), groups)

    def may_watch(self, caller: Caller) -> bool:
        """The live dashboard shows every request (masked): every signed-in person, unless the profile
        names the roles that may watch it."""
        return not self.dashboard_roles or caller.role in self.dashboard_roles


    def may_see_people(self, caller: Caller | None) -> bool:
        """Who is in which group (names) is shown to the profile's admin_roles only. In demo mode,
        on one machine, it is shown to whoever runs the demo."""
        if not self.on:
            return True
        return caller is not None and caller.role in self.admin_roles


def bearer(authorization: str | None) -> str | None:
    value = authorization or ""
    if not value.lower().startswith("bearer "):
        return None
    return value[7:].strip() or None
