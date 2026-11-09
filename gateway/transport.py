"""How the gateway reaches tool servers (ADR 030).

The repository's own tool servers (127.0.0.1, ports 8100-8107) switch to HTTPS with mutual TLS
when GOVERNED_MTLS_DIR is set (mcp_servers/common/serve.py). This module makes the same switch on
the calling side: the profile keeps its http:// addresses, and the gateway connects to those
servers over https:// with its client certificate, checking theirs against the local CA. Other
tool servers (another platform's, such as Meridale's data tools) are called as configured.
"""

import os
import ssl
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from mcp.client.streamable_http import streamablehttp_client

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


def open_mcp(url: str, **kw):
    """streamablehttp_client for a tool server, over mutual TLS when it is on for that server."""
    d = mtls_dir()
    target = tool_url(url)
    if d is None or target == url:
        return streamablehttp_client(url, **kw)
    import httpx
    ctx = client_context(d)

    def factory(headers=None, timeout=None, auth=None):
        args = {"verify": ctx, "timeout": timeout or httpx.Timeout(30.0, read=300.0)}
        if headers is not None:
            args["headers"] = headers
        if auth is not None:
            args["auth"] = auth
        return httpx.AsyncClient(**args)
    return streamablehttp_client(target, httpx_client_factory=factory, **kw)
