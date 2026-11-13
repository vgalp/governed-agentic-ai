"""Sessions launched from the EHR (ADR 035): the clinician's tokens and the chart's patient, kept on
the server only, encrypted at rest. The browser holds a random session ID and nothing else.

Several processes read a session (the API that created it, and the record tools that use its token),
so the store is shared: a folder of encrypted files on one machine, with the key from the secrets
provider (fhir/secrets.py). A multi-machine deployment swaps in a shared store with the same three
calls (save, load, delete).

A session ID is never written in a file name: the file is named by its SHA-256, so a directory
listing does not reveal live session IDs.
"""

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

REF_RE = re.compile(r"^[0-9a-f]{64}$")


def default_store(profile: str) -> "SessionStore":
    """The store the API and the record tools share for one profile (same folder, same key)."""
    from fhir.secrets import session_key
    folder = Path(os.environ.get("GAAI_SESSIONS_DIR", "~/.config/governed-agentic-ai/sessions")).expanduser()
    return SessionStore(session_key(), folder / profile)


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


class SessionStore:
    """Encrypted files, one per session, readable by the owner only. In memory when no folder is
    given (tests, a single process)."""

    def __init__(self, key: bytes, folder: str | Path | None = None, clock=time.time):
        self.fernet, self.clock = Fernet(key), clock
        self.folder = Path(folder).expanduser() if folder else None
        self._mem: dict[str, bytes] = {}
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            os.chmod(self.folder, 0o700)

    @staticmethod
    def new_key() -> bytes:
        return Fernet.generate_key()

    def _name(self, sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def ref(self, sid: str) -> str:
        """What the pipeline carries instead of the session ID: its SHA-256. It finds the encrypted
        session on this server, for the record tools, but cannot be used as the browser's cookie."""
        return self._name(sid)

    def load_ref(self, ref: str) -> LaunchSession | None:
        if not isinstance(ref, str) or not REF_RE.match(ref):
            return None
        blob = None
        if self.folder:
            path = self.folder / ref
            blob = path.read_bytes() if path.exists() else None
        else:
            blob = self._mem.get(ref)
        if blob is None:
            return None
        try:
            s = LaunchSession(**json.loads(self.fernet.decrypt(blob)))
        except (InvalidToken, ValueError, TypeError):
            return None
        return s if self.clock() < s.ends_at else None

    def save(self, s: LaunchSession) -> None:
        blob = self.fernet.encrypt(json.dumps(asdict(s)).encode())
        if not self.folder:
            self._mem[self._name(s.id)] = blob
            return
        path = self.folder / self._name(s.id)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)

    def load(self, sid: str) -> LaunchSession | None:
        if not sid or len(sid) > 200:
            return None
        name = self._name(sid)
        if self.folder:
            path = self.folder / name
            blob = path.read_bytes() if path.exists() else None
        else:
            blob = self._mem.get(name)
        if blob is None:
            return None
        try:
            data = json.loads(self.fernet.decrypt(blob))
        except (InvalidToken, ValueError):
            return None                       # another key, or a damaged file: no session
        s = LaunchSession(**data)
        if s.id != sid or self.clock() >= s.ends_at:
            self.delete(sid)
            return None
        return s

    def delete(self, sid: str) -> None:
        name = self._name(sid)
        self._mem.pop(name, None)
        if self.folder:
            (self.folder / name).unlink(missing_ok=True)

    def sweep(self) -> int:
        """Delete ended sessions; the count deleted."""
        n = 0
        if self.folder:
            for path in self.folder.iterdir():
                try:
                    data = json.loads(self.fernet.decrypt(path.read_bytes()))
                    if self.clock() < data.get("ends_at", 0):
                        continue
                except (InvalidToken, ValueError, OSError):
                    pass
                path.unlink(missing_ok=True)
                n += 1
        return n
