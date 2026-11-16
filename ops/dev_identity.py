"""A stand-in for an organization's identity system, for trying the gateway on one machine (ADR 039).

DEVELOPMENT ONLY. The governed gateway never decides who a caller is: it sends each bearer token to
the organization's own system and believes the answer (gateway/endpoint.py, ADR 015). A hospital
points it at its identity provider. To try the gateway without one, this answers that question from
a local file.

    python -m ops.dev_identity add --role clinician --name "My assistant"   # prints a token, once
    python -m ops.dev_identity list                                         # clients and roles, never tokens
    python -m ops.dev_identity serve                                        # http://127.0.0.1:8120/introspect
    python -m ops.dev_identity serve --port 8121                            # if 8120 is taken (match outside.yaml auth.url)
    python -m ops.dev_identity remove --name "My assistant"

What keeps it from being mistaken for the real thing:
- it listens on 127.0.0.1 only, and has no option to listen anywhere else;
- the file holds a SHA-256 of each token, never the token (it is shown once, when made);
- the file is ~/.config/governed-agentic-ai/gateway_dev_clients.json, readable by its owner only;
- every answer needs the shared secret the gateway sends (GATEWAY_INTROSPECT_SECRET);
- it has no expiry, no revocation list, no sign-in and no audit of its own. An identity provider has.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8120
SECRET_ENV = "GATEWAY_INTROSPECT_SECRET"
DEFAULT_FILE = Path.home() / ".config" / "governed-agentic-ai" / "gateway_dev_clients.json"
LOCATIONS = ("local", "external")
MAX_BODY = 8192


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def read_clients(path: Path) -> dict[str, dict]:
    """{token hash: client}. A missing file is no clients; a damaged one stops the command."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ValueError(f"{path} is not a clients file")
    return data


def write_clients(path: Path, clients: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(clients, f, indent=1)
    os.replace(tmp, path)                               # whole file or nothing


def add_client(path: Path, name: str, role: str, location: str = "local", make_token=secrets.token_urlsafe) -> str:
    """Add a client and return its token, which is not stored anywhere."""
    if location not in LOCATIONS:
        raise ValueError(f"location must be one of {', '.join(LOCATIONS)}")
    if not name.strip() or not role.strip():
        raise ValueError("a client needs a name and a role")
    clients = read_clients(path)
    if any(c.get("name") == name for c in clients.values()):
        raise ValueError(f"a client named {name!r} exists; remove it first")
    token = make_token(32)
    clients[digest(token)] = {"client_id": f"dev-{secrets.token_hex(4)}", "name": name, "role": role,
                              "location": location}
    write_clients(path, clients)
    return token


def remove_client(path: Path, name: str) -> int:
    clients = read_clients(path)
    kept = {k: c for k, c in clients.items() if c.get("name") != name}
    write_clients(path, kept)
    return len(clients) - len(kept)


def answer(clients: dict[str, dict], secret: str, sent_secret: str | None, body: bytes) -> tuple[int, dict]:
    """The reply to one token check: (HTTP status, JSON). Wrong secret: 401. Anything else that is not a
    known token: {"active": false}, as a real token check answers."""
    if not sent_secret or not hmac.compare_digest(sent_secret.encode(), secret.encode()):
        return 401, {"error": "unauthorized"}
    try:
        token = json.loads(body or b"{}").get("token")
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        token = None
    client = clients.get(digest(token)) if isinstance(token, str) and token else None
    if client is None:
        return 200, {"active": False}
    return 200, {"active": True, **{k: client[k] for k in ("client_id", "name", "role", "location") if k in client}}


def handler_for(path: Path, secret: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802  (the name http.server calls)
            if self.path != "/introspect":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            status, body = answer(read_clients(path), secret, self.headers.get("X-Introspect-Secret"),
                                  self.rfile.read(length))
            self._send(status, body)

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:      # one line per check, never the token
            print(f"token check: {args[1] if len(args) > 1 else ''}", flush=True)
    return Handler


def serve(path: Path, secret: str, port: int = PORT) -> None:
    try:
        server = ThreadingHTTPServer((HOST, port), handler_for(path, secret))
    except OSError as e:
        raise OSError(f"cannot listen on {HOST}:{port} ({e.strerror or e}); another program may be using it. "
                      "Use --port, and put the same port in the profile's outside.yaml auth.url") from None
    print(f"DEVELOPMENT identity stand-in listening on http://{HOST}:{port}/introspect "
          f"({len(read_clients(path))} client(s) in {path}). Not for real use.", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Development-only stand-in for an organization's identity system.")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE, help="the clients file")
    sub = ap.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add", help="add a client and print its token once")
    a.add_argument("--role", required=True, help="a role in the profile's roles.yaml, e.g. clinician")
    a.add_argument("--name", required=True, help="what to call the client, e.g. \"My assistant\"")
    a.add_argument("--location", choices=LOCATIONS, default="local",
                   help="local: runs inside the organization; external: an outside AI service (gets no record data)")
    r = sub.add_parser("remove", help="remove a client by name")
    r.add_argument("--name", required=True)
    sub.add_parser("list", help="clients and roles (never tokens)")
    s = sub.add_parser("serve", help=f"answer token checks on {HOST}")
    s.add_argument("--port", type=int, default=PORT, help=f"default {PORT}; must match outside.yaml auth.url")
    args = ap.parse_args(argv)
    try:
        if args.command == "add":
            token = add_client(args.file, args.name, args.role, args.location)
            print(f"Client {args.name!r} added with role {args.role} ({args.location}). Its token, shown once:\n{token}")
        elif args.command == "remove":
            print(f"Removed {remove_client(args.file, args.name)} client(s).")
        elif args.command == "list":
            for c in read_clients(args.file).values():
                print(f"{c.get('name')} | role {c.get('role')} | {c.get('location')} | {c.get('client_id')}")
        else:
            secret = os.environ.get(SECRET_ENV)
            if not secret:
                print(f"Set {SECRET_ENV}: the secret the gateway sends with each token check.")
                return 2
            serve(args.file, secret, args.port)
    except (ValueError, OSError) as e:
        print(f"Not done: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
