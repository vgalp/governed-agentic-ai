"""Record a FHIR sandbox's real answers, and replay them in tests (ADR 036).

The fake servers in the tests answer in the shape the vendors document. A recording answers with
what the vendor's sandbox actually sent: its envelope, paging links, OperationOutcome entries,
content types and errors. `scripts/fhir_record.py` makes one; `ReplaySession` serves it to an
unchanged `FhirClient` in place of `requests.Session`.

Format (one folder per vendor, tests/fixtures/fhir/<vendor>/):

    index.json     what was asked and what came back, in order:
                   {"format": 1, "source": "recorded" | "hand-built", "recorded_on", "profile",
                    "base_url", "patient": {...}, "exchanges": [{"path", "query", "status",
                    "content_type", "headers", "body"}]}
    bodies/*.json  each answer's body, with the server's base URL written as {{base}}

`query` is a sorted list of [name, value] pairs. A value may be a glob (`ge????-??-??T00:00:00Z`)
so a date window recorded on one day still matches the search made on another.

Recording is for sandboxes only (synthetic patients): `SANDBOX_HOSTS` lists the hosts it accepts.
Nothing here is imported by the running assistant.
"""

from __future__ import annotations

import fnmatch
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = 1
BASE = "{{base}}"
SANDBOX_HOSTS = frozenset({
    "fhir.epic.com",                 # Epic's public sandbox
    "fhir-open.cerner.com",          # Oracle Health's open sandbox
    "fhir-ehr-code.cerner.com",      # Oracle Health's secure sandbox
    "localhost", "127.0.0.1",        # a local HAPI server with synthetic data
})
KEPT_HEADERS = ("Retry-After",)      # never Authorization, Set-Cookie or anything that identifies a session
_DATE_WINDOW = re.compile(r"^(ge|gt)\d{4}-\d{2}-\d{2}T00:00:00Z$")


def is_sandbox(base_url: str) -> bool:
    return (urllib.parse.urlsplit(base_url).hostname or "").lower() in SANDBOX_HOSTS


def query_pairs(url: str, params: dict | None) -> list[list[str]]:
    """The request's query as sorted [name, value] pairs: from the URL (a 'next' link) and params."""
    pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    pairs += [(str(k), str(v)) for k, v in (params or {}).items()]
    return sorted([k, v] for k, v in pairs)


def relative_path(base_url: str, url: str) -> str:
    base = urllib.parse.urlsplit(base_url).path.rstrip("/")
    path = urllib.parse.urlsplit(url).path
    return path[len(base):] or "/" if path.startswith(base) else path


def loosen(pairs: list[list[str]]) -> list[list[str]]:
    """Each value as a glob that matches it exactly, except date windows, whose digits match any
    digit: the recording then matches the same search made on any later day."""
    return [[k, re.sub(r"\d", "?", v) if _DATE_WINDOW.match(v) else _literal(v)] for k, v in pairs]


def _literal(value: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", value)


def _swap(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_swap(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _swap(v, old, new) for k, v in value.items()}
    return value


# --- recording ----------------------------------------------------------------------------------

class RecordingSession:
    """Wraps a requests.Session: every GET goes to the server, and its answer is kept."""

    def __init__(self, inner, base_url: str):
        if not is_sandbox(base_url):
            raise ValueError(f"recording is for sandboxes with synthetic patients only: "
                             f"{urllib.parse.urlsplit(base_url).hostname} is not one of {sorted(SANDBOX_HOSTS)}")
        self.inner, self.base = inner, base_url.rstrip("/")
        self.exchanges: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        r = self.inner.get(url, params=params, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = {"_text": r.text[:2000]}
        self.exchanges.append({
            "path": relative_path(self.base, url),
            "query": loosen(query_pairs(url, params)),
            "status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "headers": {h: r.headers[h] for h in KEPT_HEADERS if h in r.headers},
            "body": _swap(body, self.base, BASE),
        })
        return r

    def post(self, *args, **kwargs):              # the token endpoint: passed through, never recorded
        return self.inner.post(*args, **kwargs)

    def save(self, folder: Path, meta: dict) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        bodies = folder / "bodies"
        bodies.mkdir(exist_ok=True)
        for old in bodies.glob("*.json"):
            old.unlink()
        index = []
        for i, ex in enumerate(self.exchanges):
            name = f"{i:03d}-{re.sub(r'[^A-Za-z]', '', ex['path'].split('/')[1] if '/' in ex['path'] else 'x')}.json"
            (bodies / name).write_text(json.dumps(ex["body"], indent=1, sort_keys=True) + "\n")
            index.append({**{k: v for k, v in ex.items() if k != "body"}, "body": f"bodies/{name}"})
        path = folder / "index.json"
        path.write_text(json.dumps({"format": FORMAT, **meta, "base_url": self.base, "exchanges": index},
                                   indent=1) + "\n")
        return path


# --- replaying ----------------------------------------------------------------------------------

@dataclass
class ReplayResponse:
    status_code: int
    _body: Any
    headers: dict[str, str]

    def json(self):
        if isinstance(self._body, dict) and set(self._body) == {"_text"}:
            raise ValueError("not JSON")
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


class UnrecordedRequest(AssertionError):
    """The code asked for something the recording does not have: a test must not pass by accident."""


class ReplaySession:
    """Answers GETs from a recording. Each request must match a recorded one (path and query);
    a request matching none raises UnrecordedRequest. `requests` lists what was asked."""

    def __init__(self, folder: str | Path, base_url: str | None = None):
        self.folder = Path(folder)
        self.index = json.loads((self.folder / "index.json").read_text())
        if self.index.get("format") != FORMAT:
            raise ValueError(f"{self.folder}: recording format {self.index.get('format')}, expected {FORMAT}")
        self.base = (base_url or self.index["base_url"]).rstrip("/")
        self.requests: list[tuple[str, list[list[str]]]] = []

    @property
    def meta(self) -> dict:
        return {k: v for k, v in self.index.items() if k != "exchanges"}

    def get(self, url, params=None, headers=None, timeout=None):
        path, query = relative_path(self.base, url), query_pairs(url, params)
        self.requests.append((path, query))
        for ex in self.index["exchanges"]:
            if ex["path"] == path and _matches(ex["query"], query):
                body = json.loads((self.folder / ex["body"]).read_text())
                headers = {"Content-Type": ex.get("content_type") or "application/fhir+json", **ex.get("headers", {})}
                return ReplayResponse(ex["status"], _swap(body, BASE, self.base), headers)
        raise UnrecordedRequest(f"not in the recording {self.folder.name}: GET {path} {query}")


def _matches(recorded: list[list[str]], asked: list[list[str]]) -> bool:
    return len(recorded) == len(asked) and all(
        rk == ak and fnmatch.fnmatchcase(av, rv) for (rk, rv), (ak, av) in zip(recorded, asked))
