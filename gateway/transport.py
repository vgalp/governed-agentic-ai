"""How the gateway reaches tool servers (ADR 030).

The repository's own tool servers (127.0.0.1, ports 8100-8107) switch to HTTPS with mutual TLS
when GOVERNED_MTLS_DIR is set (mcp_servers/common/serve.py). This module makes the same switch on
the calling side: the profile keeps its http:// addresses, and the gateway connects to those
servers over https:// with its client certificate, checking theirs against the local CA. Other
tool servers (another platform's, such as Meridale's data tools) are called as configured.
"""

import contextlib
import os
import ssl
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from mcp import ClientSession
from mcp.client.streamable_http import httpx2, streamable_http_client

OWN_PORTS = set(range(8100, 8108))   # the tool servers in mcp_servers/ (scripts/dev.sh SERVICES)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def mtls_dir() -> Path | None:
    d = os.environ.get("GOVERNED_MTLS_DIR")
    return Path(d) if d else None


def is_own_server(url: str) -> bool:
    u = urlparse(url)
    return u.hostname in LOOPBACK and u.port in OWN_PORTS


def tool_url(url: str) -> str:
    """The address to call: https:// for the repository's own servers when mTLS is on."""
    if mtls_dir() is None or not is_own_server(url):
        return url
    return urlunparse(urlparse(url)._replace(scheme="https"))


def client_context(directory: Path) -> ssl.SSLContext:
    """Trusts only the local CA, and presents the gateway's certificate."""
    files = {"ca": directory / "ca.crt", "cert": directory / "gateway.crt", "key": directory / "gateway.key"}
    missing = [str(f) for f in files.values() if not f.is_file()]
    if missing:
        raise RuntimeError(f"mTLS is on but these files are missing: {', '.join(missing)}. Run ./scripts/certs.sh")
    ctx = ssl.create_default_context(cafile=str(files["ca"]))
    ctx.load_cert_chain(str(files["cert"]), str(files["key"]))
    return ctx


@contextlib.asynccontextmanager
async def mcp_session(url: str, *, connect_timeout: float = 30.0, read_timeout: float = 300.0,
                      headers: dict[str, str] | None = None):
    """An MCP session with a server, ready to use: the one place the MCP library's client is named.

    Over mutual TLS when it is on for that server. `headers` go on every request (a caller's bearer token).
    Each request stands alone, so no session is ended on close."""
    d, target = mtls_dir(), tool_url(url)
    verify = client_context(d) if d is not None and target != url else True
    async with httpx2.AsyncClient(headers=headers, verify=verify,
                                  timeout=httpx2.Timeout(connect_timeout, read=read_timeout)) as http:
        async with streamable_http_client(target, http_client=http, terminate_on_close=False) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
                await session.initialize()
                yield session


def tool_text(result) -> tuple[str, bool]:
    """(the text of a tool's answer, whether the server reported an error)."""
    content = getattr(result, "content", None) or []
    text = content[0].text if content and hasattr(content[0], "text") else ""
    return text, bool(getattr(result, "is_error", False))
