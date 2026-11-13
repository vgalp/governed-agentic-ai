"""Sessions launched from the EHR (ADR 035, ADR 036): the clinician's tokens and the chart's patient,
kept on the server only, encrypted at rest. The browser holds a random session ID and nothing else.

Several processes read a session (the API that created it, and the record tools that use its
token), and in a deployment with more than one server, several machines do. Where the encrypted
sessions live is a `BlobStore`, chosen by `GAAI_SESSIONS_URL`:

    (unset)                      a folder on this machine (GAAI_SESSIONS_DIR), files 0600 in a 0700 folder
    rediss://host:6380/0         Redis over TLS, shared by every server (Azure Cache, ElastiCache ...)
    redis://localhost:6379/0     Redis without TLS: refused unless the host is this machine
    env:NAME, file:..., azure-keyvault:..., aws-secretsmanager:...
                                 the URL itself from the secrets provider (it holds the password)

Whatever the store, a session is encrypted here (Fernet, the key from fhir/secrets.py) before it is
stored, so the store never holds a readable token; and it is stored under the SHA-256 of its ID, so
a key listing does not reveal live session IDs. Redis also expires a session at its end.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

REF_RE = re.compile(r"^[0-9a-f]{64}$")


class SessionStoreError(Exception):
    """The session store is misconfigured or cannot be reached."""


def default_store(profile: str, environ=os.environ) -> "SessionStore":
    """The store the API and the record tools share for one profile (same place, same key)."""
    from fhir.secrets import session_key
    url = environ.get("GAAI_SESSIONS_URL")
    if url:
        return SessionStore(session_key(), backend=RedisBlobs.from_url(_resolve(url, environ), prefix=f"gaai:{profile}:"))
    folder = Path(environ.get("GAAI_SESSIONS_DIR", "~/.config/governed-agentic-ai/sessions")).expanduser()
    return SessionStore(session_key(), folder / profile)


def _resolve(url: str, environ) -> str:
    """A Redis URL as given, or read from the secrets provider when it is a reference."""
    if url.startswith(("redis://", "rediss://")):
        return url
    from fhir.secrets import SecretError, get
    try:
        return get(url, environ)
    except SecretError as e:
        raise SessionStoreError(f"GAAI_SESSIONS_URL: {e}") from None


@dataclass
class LaunchSession:
    id: str
    iss: str
    patient: str
    encounter: str | None
    user: str                       # fhirUser, e.g. Practitioner/abc
    name: str
    role: str
    access_token: str
    refresh_token: str | None
    expires_at: float               # the access token
    scope: str
    token_endpoint: str
    created: float
    ends_at: float                  # the session, whatever the token


# --- where encrypted sessions are kept ----------------------------------------------------------------

class BlobStore(Protocol):
    """Opaque encrypted blobs under hex names. `ttl` is seconds until the store may drop the blob."""

    def get(self, name: str) -> bytes | None: ...
    def put(self, name: str, blob: bytes, ttl: float) -> None: ...
    def delete(self, name: str) -> None: ...
    def names(self) -> list[str]: ...


class MemoryBlobs:
    """One process only: tests, a single-process demo."""

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> bytes | None:
        with self._lock:
            return self._items.get(name)

    def put(self, name: str, blob: bytes, ttl: float) -> None:
        with self._lock:
            self._items[name] = blob

    def delete(self, name: str) -> None:
        with self._lock:
            self._items.pop(name, None)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._items)


class FileBlobs:
    """One machine: a folder of owner-only files, each written whole or not at all."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)

    def get(self, name: str) -> bytes | None:
        path = self.folder / name
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put(self, name: str, blob: bytes, ttl: float) -> None:
        path = self.folder / name
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)

    def delete(self, name: str) -> None:
        (self.folder / name).unlink(missing_ok=True)

    def names(self) -> list[str]:
        return [p.name for p in self.folder.iterdir() if REF_RE.match(p.name)]


class RedisBlobs:
    """Several machines: one Redis, keys `<prefix><name>`, expiring with the session.

    `client` is a redis.Redis (or anything with get/set/delete/scan_iter); `from_url` makes one.
    The redis package is imported only here, so a single-machine install does not need it.
    """

    def __init__(self, client, prefix: str = "gaai:"):
        self.client, self.prefix = client, prefix

    @classmethod
    def from_url(cls, url: str, prefix: str = "gaai:", timeout: float = 5.0) -> "RedisBlobs":
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("redis", "rediss"):
            raise SessionStoreError("GAAI_SESSIONS_URL must be a redis:// or rediss:// URL")
        if u.scheme == "redis" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise SessionStoreError("GAAI_SESSIONS_URL: use rediss:// (TLS) for a Redis on another machine")
        try:
            import redis
        except ImportError:
            raise SessionStoreError("GAAI_SESSIONS_URL is set but the redis package is not installed "
                                    "(uv sync --group deploy)") from None
        return cls(redis.Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout), prefix)

    def _key(self, name: str) -> str:
        return self.prefix + name

    def get(self, name: str) -> bytes | None:
        return self._call(self.client.get, self._key(name))

    def put(self, name: str, blob: bytes, ttl: float) -> None:
        self._call(self.client.set, self._key(name), blob, ex=max(1, int(ttl)))

    def delete(self, name: str) -> None:
        self._call(self.client.delete, self._key(name))

    def names(self) -> list[str]:
        keys = self._call(lambda: list(self.client.scan_iter(match=self.prefix + "*", count=500)))
        out = [(k.decode() if isinstance(k, bytes) else k).removeprefix(self.prefix) for k in keys]
        return [n for n in out if REF_RE.match(n)]

    @staticmethod
    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:      # the redis package's errors, without importing it here
            raise SessionStoreError(f"the session store did not answer ({type(e).__name__})") from None


class SessionStore:
    """Launched sessions, encrypted, in a `BlobStore`.

    `SessionStore(key)` keeps them in memory; `SessionStore(key, folder)` in a folder;
    `SessionStore(key, backend=...)` anywhere else (Redis).
    """

    def __init__(self, key: bytes, folder: str | Path | None = None, clock=time.time,
                 backend: BlobStore | None = None):
        if folder is not None and backend is not None:
            raise ValueError("give a folder or a backend, not both")
        self.fernet, self.clock = Fernet(key), clock
        self.folder = Path(folder).expanduser() if folder else None
        self.backend: BlobStore = backend or (FileBlobs(self.folder) if self.folder else MemoryBlobs())

    @staticmethod
    def new_key() -> bytes:
        return Fernet.generate_key()

    def _name(self, sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def ref(self, sid: str) -> str:
        """What the pipeline carries instead of the session ID: its SHA-256. It finds the encrypted
        session on this server, for the record tools, but cannot be used as the browser's cookie."""
        return self._name(sid)

    def _open(self, name: str) -> LaunchSession | None:
        blob = self.backend.get(name)
        if blob is None:
            return None
        try:
            return LaunchSession(**json.loads(self.fernet.decrypt(blob)))
        except (InvalidToken, ValueError, TypeError):
            return None                       # another key, or a damaged entry: no session

    def load_ref(self, ref: str) -> LaunchSession | None:
        if not isinstance(ref, str) or not REF_RE.match(ref):
            return None
        s = self._open(ref)
        return s if s is not None and self.clock() < s.ends_at else None

    def save(self, s: LaunchSession) -> None:
        blob = self.fernet.encrypt(json.dumps(asdict(s)).encode())
        self.backend.put(self._name(s.id), blob, ttl=max(1.0, s.ends_at - self.clock()))

    def load(self, sid: str) -> LaunchSession | None:
        if not sid or len(sid) > 200:
            return None
        s = self._open(self._name(sid))
        if s is None:
            return None
        if s.id != sid or self.clock() >= s.ends_at:
            self.delete(sid)
            return None
        return s

    def delete(self, sid: str) -> None:
        self.backend.delete(self._name(sid))

    def sweep(self) -> int:
        """Delete ended or unreadable sessions; the count deleted. (Redis also expires them itself.)"""
        n = 0
        for name in self.backend.names():
            blob = self.backend.get(name)
            try:
                if blob is not None and self.clock() < json.loads(self.fernet.decrypt(blob)).get("ends_at", 0):
                    continue
            except (InvalidToken, ValueError):
                pass
            self.backend.delete(name)
            n += 1
        return n
