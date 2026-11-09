"""OPA's token and mutual TLS to the tool servers (audit SC-5, SC-6; ADR 030)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from gateway import opa, transport
from mcp_servers.common import serve

ROOT = Path(__file__).resolve().parent.parent


# --- OPA token ------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_token(monkeypatch):
    opa.opa_token.cache_clear()
    yield
    opa.opa_token.cache_clear()


def test_the_opa_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPA_TOKEN", "abc")
    assert opa._headers() == {"Authorization": "Bearer abc"}


def test_the_opa_token_falls_back_to_the_repositorys_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPA_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("AUDIT_HMAC_KEY=x\nOPA_TOKEN=from-file\n")
    monkeypatch.setattr(opa, "ENV_FILE", env)
    assert opa.opa_token() == "from-file"


def test_without_a_token_no_header_is_sent(monkeypatch, tmp_path):
    monkeypatch.delenv("OPA_TOKEN", raising=False)
    monkeypatch.setattr(opa, "ENV_FILE", tmp_path / "missing")
    assert opa._headers() == {}


def test_every_opa_call_carries_the_token(monkeypatch):
    monkeypatch.setenv("OPA_TOKEN", "t0k")
    seen = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"x": 1}}

    def record(method):
        def call(url, **kw):
            seen.append((method, kw.get("headers")))
            return Resp()
        return call
    monkeypatch.setattr(opa.requests, "get", record("get"))
    monkeypatch.setattr(opa.requests, "put", record("put"))
    monkeypatch.setattr(opa.requests, "post", record("post"))

    class P:
        name = "clinic"

        def policy_data(self):
            return {"y": 2}                                   # differs, so the PUT happens too
    opa.decide(P(), "mcp/authz/allow", {})
    assert [m for m, _ in seen] == ["get", "put", "post"]
    assert all(h == {"Authorization": "Bearer t0k"} for _, h in seen)


@pytest.mark.skipif(not shutil.which("opa"), reason="the opa binary is not installed")
def test_the_authorization_policy_passes_its_tests():
    out = subprocess.run(["opa", "test", str(ROOT / "policies" / "system")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_opa_is_started_with_token_authentication():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "--authentication=token" in compose and "--authorization=basic" in compose
    assert "OPA_TOKEN: ${OPA_TOKEN:-}" in compose


# --- mutual TLS -----------------------------------------------------------------------------------

def test_only_the_repositorys_own_servers_switch_to_https(monkeypatch, tmp_path):
    monkeypatch.setenv("GOVERNED_MTLS_DIR", str(tmp_path))
    assert transport.tool_url("http://127.0.0.1:8101/mcp") == "https://127.0.0.1:8101/mcp"
    assert transport.tool_url("http://localhost:8107/mcp") == "https://localhost:8107/mcp"
    assert transport.tool_url("http://127.0.0.1:8201/mcp") == "http://127.0.0.1:8201/mcp"     # another platform's
    assert transport.tool_url("http://10.0.0.5:8101/mcp") == "http://10.0.0.5:8101/mcp"       # not on this machine
    monkeypatch.delenv("GOVERNED_MTLS_DIR")
    assert transport.tool_url("http://127.0.0.1:8101/mcp") == "http://127.0.0.1:8101/mcp"     # off: unchanged


def test_missing_certificates_stop_the_server_and_the_gateway_with_the_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("GOVERNED_MTLS_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="certs.sh"):
        serve.tls_files()
    with pytest.raises(RuntimeError, match="certs.sh"):
        transport.client_context(tmp_path)
    monkeypatch.delenv("GOVERNED_MTLS_DIR")
    assert serve.tls_files() is None


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is not installed")
def test_the_certificate_script_makes_a_ca_server_and_gateway_certificate(tmp_path):
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "certs.sh", work / "scripts" / "certs.sh")
    out = subprocess.run(["bash", "scripts/certs.sh"], cwd=work, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    d = work / "certs"
    for f in ("ca.crt", "server.crt", "server.key", "gateway.crt", "gateway.key"):
        assert (d / f).is_file()
    assert oct((d / "gateway.key").stat().st_mode)[-3:] == "600"
    ok = subprocess.run(["openssl", "verify", "-x509_strict", "-CAfile", "ca.crt", "server.crt", "gateway.crt"],
                        cwd=d, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    ctx = transport.client_context(d)                        # the gateway can load what it needs
    assert ctx.verify_mode.name == "CERT_REQUIRED"
    again = subprocess.run(["bash", "scripts/certs.sh"], cwd=work, capture_output=True, text=True)
    assert "already exists" in again.stdout                  # never overwritten by accident


def test_every_tool_server_starts_through_the_shared_serve():
    for f in sorted((ROOT / "mcp_servers").glob("*/server.py")):
        text = f.read_text()
        assert "serve(mcp)" in text and 'mcp.run(transport="streamable-http")' not in text, f.parent.name
