"""Where secrets come from (ADR 035): client secrets, private keys and the session key, named by a
reference, never written in a profile.

    env:EPIC_CLIENT_SECRET                                  an environment variable (.env)
    file:~/.config/governed-agentic-ai/epic/privatekey.pem  a file readable by its owner only
    azure-keyvault:https://my-vault.vault.azure.net/secrets/epic-client-secret
    aws-secretsmanager:arn:aws:secretsmanager:ca-central-1:123456789012:secret:epic-key
    aws-secretsmanager:epic-key                             (by name, in AWS_REGION)

The cloud ones sign in with the machine's own identity (Azure managed identity or `az login`;
the AWS role or profile), so no further secret is needed to reach the vault. Their libraries are
optional: installed only where they are used (`uv add azure-identity azure-keyvault-secrets`, or
`uv add boto3`). Values are cached for a few minutes, so a rotated secret is picked up without a restart.
"""

import os
import stat
import threading
import time
import urllib.parse
from pathlib import Path

CACHE_SECONDS = 300
SCHEMES = ("env", "file", "azure-keyvault", "aws-secretsmanager")


class SecretError(Exception):
    pass


_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def check_ref(ref: str) -> str:
    """The reference's scheme, or an error saying what is accepted. Used by the profile loader."""
    scheme, _, rest = (ref or "").partition(":")
    if scheme not in SCHEMES or not rest:
        raise SecretError(f"a secret is named as {' / '.join(s + ':…' for s in SCHEMES)}, not {ref!r}")
    if scheme == "azure-keyvault":
        u = urllib.parse.urlsplit(rest)
        if u.scheme != "https" or not (u.hostname or "").endswith(".vault.azure.net") or "/secrets/" not in u.path:
            raise SecretError(f"an Azure Key Vault secret is https://<vault>.vault.azure.net/secrets/<name>: {rest}")
    return scheme


def get(ref: str, environ=os.environ, clock=time.time, fetchers: dict | None = None) -> str:
    """The secret's value. `fetchers` replaces the cloud clients (tests)."""
    scheme = check_ref(ref)
    rest = ref.split(":", 1)[1]
    if scheme == "env":
        v = environ.get(rest)
        if not v:
            raise SecretError(f"set {rest} (in .env)")
        return v
    if scheme == "file":
        return _file(rest)
    with _lock:
        hit = _cache.get(ref)
        if hit and clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
    fetch = (fetchers or {}).get(scheme) or {"azure-keyvault": _azure, "aws-secretsmanager": _aws}[scheme]
    value = fetch(rest)
    if not value:
        raise SecretError(f"the secret {ref} is empty")
    with _lock:
        _cache[ref] = (clock(), value)
    return value


def _file(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SecretError(f"secret file not found: {p}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise SecretError(f"{p} can be read by others (mode {oct(mode)}): chmod 600 {p}")
    return p.read_text().strip() if p.suffix != ".pem" else p.read_text()


def _azure(url: str) -> str:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        raise SecretError("Azure Key Vault needs: uv add azure-identity azure-keyvault-secrets") from None
    u = urllib.parse.urlsplit(url)
    parts = [x for x in u.path.split("/") if x]          # secrets/<name>[/<version>]
    client = SecretClient(f"https://{u.hostname}", DefaultAzureCredential())
    try:
        return client.get_secret(parts[1], parts[2] if len(parts) > 2 else None).value
    except Exception as e:  # noqa: BLE001  the SDK raises many kinds; the message says which secret
        raise SecretError(f"Azure Key Vault: could not read {parts[1]} ({type(e).__name__})") from None


def _aws(name: str) -> str:
    try:
        import boto3
    except ImportError:
        raise SecretError("AWS Secrets Manager needs: uv add boto3") from None
    region = name.split(":")[3] if name.startswith("arn:") else os.environ.get("AWS_REGION")
    try:
        return boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=name)["SecretString"]
    except Exception as e:  # noqa: BLE001
        raise SecretError(f"AWS Secrets Manager: could not read the secret ({type(e).__name__})") from None


def session_key(ref: str | None = None, environ=os.environ) -> bytes:
    """The key sessions are encrypted with. Named by GAAI_SESSION_KEY (any reference above); on one
    machine, without it, a key file is made once (owner-only) so sessions survive a restart."""
    ref = ref or environ.get("GAAI_SESSION_KEY_REF")
    if ref:
        return get(ref, environ).strip().encode()
    from fhir.sessions import SessionStore
    p = Path("~/.config/governed-agentic-ai/session.key").expanduser()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(SessionStore.new_key())
    return _file(str(p)).encode()
