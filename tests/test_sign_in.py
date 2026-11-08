"""Sign-in (ADR 028): who is asking comes from a verified token in oidc mode, never from the request;
anything that does not verify gives no access; groups map to the profile's roles."""

import json
import time
from pathlib import Path

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from auth.sign_in import Caller, OidcVerifier, SignIn, SignInError, SignInRequired, bearer, role_for
from profiles import schema
from profiles.loader import ProfileError, load_profile, load_profile_from

ISSUER = "http://127.0.0.1:8180/realms/governed-demo"
SETTINGS = {"mode": "oidc", "issuer": ISSUER, "audience": "governed-api", "client_id": "governed-ui",
            "roles": {"clinicians": "clinician", "nurses": "nurse", "front-desk": "front_desk"}}
UI = Path(__file__).resolve().parent.parent / "ui"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Provider:
    """A fake identity provider: discovery, keys, and tokens it signs."""

    def __init__(self, issuer=ISSUER):
        self.issuer, self.calls = issuer, []
        self.keys = {"k1": rsa_key()}

    def jwk(self, kid, key):
        d = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
        return {**d, "kid": kid, "use": "sig", "alg": "RS256"}

    def fetch(self, url):
        self.calls.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": self.issuer, "jwks_uri": f"{self.issuer}/certs",
                    "authorization_endpoint": f"{self.issuer}/auth", "token_endpoint": f"{self.issuer}/token"}
        if url.endswith("/certs"):
            return {"keys": [self.jwk(k, v) for k, v in self.keys.items()]}
        raise requests.ConnectionError(url)

    def token(self, kid="k1", **claims):
        now = int(time.time())
        body = {"iss": self.issuer, "sub": "u-1", "aud": "governed-api", "iat": now, "exp": now + 300,
                "preferred_username": "nurse.kim", "name": "Sam Kim", "groups": ["nurses"], **claims}
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, self.keys[kid], algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def idp():
    return Provider()


def sign_in(idp, settings=SETTINGS, **env):
    return SignIn(settings, env=env, fetch_json=idp.fetch)


# --- demo mode -------------------------------------------------------------------------------------

def test_demo_mode_takes_the_person_and_role_from_the_request():
    s = SignIn(None, env={})
    assert not s.on and s.public_config() == {"mode": "demo"}
    assert s.caller(None, "front-1", "front_desk") == Caller("front-1", "front-1", "front_desk")
    assert s.caller(None).user_id == "demo-user"


def test_the_environment_turns_sign_in_on_or_off_without_editing_the_profile(idp):
    assert sign_in(idp, {**SETTINGS, "mode": "demo"}, GOVERNED_SIGN_IN="oidc").on
    assert not sign_in(idp, SETTINGS, GOVERNED_SIGN_IN="demo").on
    with pytest.raises(SignInError):
        sign_in(idp, SETTINGS, GOVERNED_SIGN_IN="maybe")


def test_oidc_without_its_settings_refuses_to_start(idp):
    for missing in ("issuer", "audience", "client_id", "roles"):
        with pytest.raises(SignInError):
            sign_in(idp, {k: v for k, v in SETTINGS.items() if k != missing})


def test_a_remote_issuer_must_be_https(idp):
    with pytest.raises(SignInError):
        sign_in(idp, {**SETTINGS, "issuer": "http://login.example.org/realms/x"})
    assert sign_in(idp, {**SETTINGS, "issuer": "https://login.example.org/realms/x"}).on


# --- oidc mode -------------------------------------------------------------------------------------

def test_the_person_and_role_come_from_the_token_never_from_the_request(idp):
    s = sign_in(idp)
    c = s.caller(f"Bearer {idp.token()}", demo_user="someone-else", demo_role="clinician")
    assert c == Caller("nurse.kim", "Sam Kim", "nurse", ("nurses",))


def test_keycloak_group_paths_and_the_first_mapped_group_decide_the_role(idp):
    s = sign_in(idp)
    assert s.caller(f"Bearer {idp.token(groups=['/front-desk'])}").role == "front_desk"
    # In two mapped groups: the first entry of sign_in.roles wins (the administrator orders it).
    assert s.caller(f"Bearer {idp.token(groups=['nurses', 'clinicians'])}").role == "clinician"
    assert role_for(("nurses", "clinicians"), {"nurses": "nurse", "clinicians": "clinician"}) == "nurse"


def test_a_person_in_no_mapped_group_has_no_role(idp):
    s = sign_in(idp)
    assert s.caller(f"Bearer {idp.token(groups=['volunteers'])}").role is None
    assert s.caller(f"Bearer {idp.token(groups=None)}").role is None


def test_groups_can_come_from_a_nested_claim(idp):
    s = sign_in(idp, {**SETTINGS, "groups_claim": "realm_access.roles"})
    c = s.caller(f"Bearer {idp.token(groups=None, realm_access={'roles': ['clinicians']})}")
    assert c.role == "clinician"


def test_the_user_falls_back_to_sub(idp):
    assert sign_in(idp).caller(f"Bearer {idp.token(preferred_username=None)}").user_id == "u-1"


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer   ", "Basic abc", "Bearer not-a-token"])
def test_no_token_or_a_malformed_one_is_refused(idp, header):
    with pytest.raises(SignInRequired):
        sign_in(idp).caller(header)


@pytest.mark.parametrize("claims", [
    {"exp": int(time.time()) - 120},                         # expired (beyond the 30 s leeway)
    {"iss": "http://127.0.0.1:8180/realms/other"},           # another issuer
    {"aud": "some-other-service", "azp": "other-client"},    # issued for someone else
    {"sub": None},                                           # a required claim missing
])
def test_tokens_that_do_not_verify_are_refused(idp, claims):
    with pytest.raises(SignInRequired):
        sign_in(idp).caller(f"Bearer {idp.token(**claims)}")


def test_the_audience_may_be_in_a_list_or_the_authorized_party(idp):
    s = sign_in(idp)
    assert s.caller(f"Bearer {idp.token(aud=['account', 'governed-api'])}").user_id == "nurse.kim"
    assert s.caller(f"Bearer {idp.token(aud='account', azp='governed-api')}").user_id == "nurse.kim"


def test_a_token_signed_by_another_key_is_refused(idp):
    forged = Provider()                                      # same issuer and kid, different key
    with pytest.raises(SignInRequired):
        sign_in(idp).caller(f"Bearer {forged.token()}")


def test_unsigned_and_shared_secret_tokens_are_refused(idp):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "x", "aud": "governed-api", "iat": now, "exp": now + 60, "groups": ["clinicians"]}
    unsigned = jwt.encode(claims, None, algorithm="none")
    hs = jwt.encode(claims, "a-shared-secret-that-anyone-could-hold-32b", algorithm="HS256", headers={"kid": "k1"})
    for token in (unsigned, hs):
        with pytest.raises(SignInRequired):
            sign_in(idp).caller(f"Bearer {token}")


def test_rotated_keys_are_fetched_again(idp):
    s = sign_in(idp)
    assert s.caller(f"Bearer {idp.token()}").role == "nurse"
    idp.keys["k2"] = rsa_key()                               # the provider rotates its keys
    s.verifier._keys_at -= 120                               # a minute has passed since the last fetch
    assert s.caller(f"Bearer {idp.token(kid='k2')}").role == "nurse"
    assert sum(u.endswith("/certs") for u in idp.calls) == 2


def test_an_unknown_key_does_not_refetch_more_than_once_a_minute(idp):
    s = sign_in(idp)
    s.caller(f"Bearer {idp.token()}")
    other = Provider()
    other.keys = {"k9": rsa_key()}
    for _ in range(5):
        with pytest.raises(SignInRequired):
            s.caller(f"Bearer {other.token(kid='k9')}")
    assert sum(u.endswith("/certs") for u in idp.calls) == 1


def test_elliptic_curve_keys_work(idp):
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = {**json.loads(ECAlgorithm.to_jwk(key.public_key())), "kid": "e1", "use": "sig"}
    fetch = idp.fetch

    def fetch_ec(url):
        d = fetch(url)
        return {"keys": d["keys"] + [jwk]} if url.endswith("/certs") else d
    s = SignIn(SETTINGS, env={}, fetch_json=fetch_ec)
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "sub": "s", "aud": "governed-api", "iat": now, "exp": now + 60,
                        "groups": ["clinicians"]}, key, algorithm="ES256", headers={"kid": "e1"})
    assert s.caller(f"Bearer {token}").role == "clinician"


def test_a_provider_that_cannot_be_reached_or_names_another_issuer_gives_no_access(idp):
    def down(url):
        raise requests.ConnectionError(url)
    with pytest.raises(SignInRequired):
        SignIn(SETTINGS, env={}, fetch_json=down).caller(f"Bearer {idp.token()}")
    liar = Provider(issuer="http://127.0.0.1:8180/realms/evil")
    with pytest.raises(SignInRequired):
        SignIn(SETTINGS, env={}, fetch_json=liar.fetch).caller(f"Bearer {idp.token()}")


def test_the_pages_get_public_settings_only(idp):
    cfg = sign_in(idp).public_config()
    assert cfg == {"mode": "oidc", "issuer": ISSUER, "client_id": "governed-ui", "scope": "openid profile"}


def test_the_dashboard_can_be_limited_to_named_roles(idp):
    s = sign_in(idp, {**SETTINGS, "dashboard_roles": ["clinician"]})
    assert not s.may_watch(s.caller(f"Bearer {idp.token()}"))
    assert s.may_watch(s.caller(f"Bearer {idp.token(groups=['clinicians'])}"))
    assert sign_in(idp).may_watch(Caller("x", "x", None))


def test_bearer_parsing():
    assert bearer("Bearer abc") == "abc" and bearer("bearer  abc ") == "abc"
    assert bearer("Basic abc") is None and bearer(None) is None


def test_the_verifier_accepts_a_trailing_slash_in_the_issuer(idp):
    v = OidcVerifier(ISSUER + "/", "governed-api", idp.fetch)
    assert v.verify(idp.token())["sub"] == "u-1"


# --- the profile ------------------------------------------------------------------------------------

def test_the_clinic_profile_has_sign_in_in_demo_mode_by_default():
    p = load_profile("clinic-assistant")
    assert p.sign_in["mode"] == "demo"
    assert set(p.sign_in["roles"].values()) <= set(p.roles)


def _copy_profile(tmp_path, edit):
    import shutil
    src = load_profile("clinic-assistant").path
    dst = tmp_path / "clinic-assistant"
    shutil.copytree(src, dst)
    f = dst / "profile.yaml"
    f.write_text(edit(f.read_text()))
    return dst


def test_a_group_mapped_to_an_unknown_role_is_refused(tmp_path):
    dst = _copy_profile(tmp_path, lambda t: t.replace("nurses: nurse", "nurses: head_nurse"))
    with pytest.raises(ProfileError, match="head_nurse"):
        load_profile_from(dst)


def test_oidc_mode_in_the_profile_needs_its_issuer(tmp_path):
    dst = _copy_profile(tmp_path, lambda t: t.replace("  mode: demo", "  mode: oidc").replace(
        "  issuer: http://127.0.0.1:8180/realms/governed-demo\n", ""))
    with pytest.raises(ProfileError, match="issuer"):
        load_profile_from(dst)


def test_unknown_sign_in_settings_are_refused():
    with pytest.raises(schema.SchemaError):
        schema.validate(schema.SignIn, {"mode": "oidc", "client_secret": "x"}, "sign_in")


# --- the pages --------------------------------------------------------------------------------------

@pytest.mark.parametrize("page", ["demo.html", "dashboard.html", "index.html"])
def test_every_page_loads_sign_in_before_its_own_script(page):
    html = (UI / page).read_text()
    assert html.index('<script src="/auth.js"></script>') < html.index("<script>\n")
    assert "new EventSource(" not in html          # the stream goes through GovAuth (a header, not the URL)


def test_the_sign_in_script_keeps_no_secret_and_uses_pkce():
    js = (UI / "auth.js").read_text()
    assert "code_challenge_method" in js and "S256" in js
    assert "client_secret" not in js and "localStorage" not in js
