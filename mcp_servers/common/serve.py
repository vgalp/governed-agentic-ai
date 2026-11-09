"""Start a tool server: one way for all of them (ADR 030).

Plain HTTP on 127.0.0.1 by default. With GOVERNED_MTLS_DIR set (scripts/dev.sh sets it when
GOVERNED_MTLS=on), the server speaks HTTPS and accepts only clients that present a certificate
signed by the local CA, which only the gateway holds (audit SC-6): another process on the machine
can no longer call a tool directly and pass its own tenant or data classes.
"""

import os
import ssl
from pathlib import Path


def tls_files() -> dict | None:
    """The CA, certificate and key for the server, or None when mTLS is off."""
    d = os.environ.get("GOVERNED_MTLS_DIR")
    if not d:
        return None
    p = Path(d)
    files = {"ca": p / "ca.crt", "cert": p / "server.crt", "key": p / "server.key"}
    missing = [str(f) for f in files.values() if not f.is_file()]
    if missing:
        raise SystemExit(f"GOVERNED_MTLS_DIR is set but these files are missing: {', '.join(missing)}. "
                         "Run ./scripts/certs.sh")
    return files


def serve(mcp) -> None:
    """Run a FastMCP server over streamable HTTP, with mutual TLS when it is configured."""
    files = tls_files()
    if files is None:
        mcp.run(transport="streamable-http")
        return
    import uvicorn
    print(f"{mcp.name}: mutual TLS on https://{mcp.settings.host}:{mcp.settings.port} "
          "(only the gateway's certificate is accepted)", flush=True)
    uvicorn.run(mcp.streamable_http_app(), host=mcp.settings.host, port=mcp.settings.port,
                log_level=mcp.settings.log_level.lower(),
                ssl_certfile=str(files["cert"]), ssl_keyfile=str(files["key"]),
                ssl_ca_certs=str(files["ca"]), ssl_cert_reqs=ssl.CERT_REQUIRED)
