"""The gateway by itself (ADR 039): the FHIR profiles offer their record tools to outside assistants,
read-only, and a development stand-in answers "who is this token" for a trial on one machine."""

import importlib.util
import json
import stat
import subprocess
from pathlib import Path

import pytest

from gateway.endpoint import Introspector
from ops import dev_identity
from profiles.loader import load_profile

ROOT = Path(__file__).resolve().parent.parent
RECORD_TOOLS = ("search_records", "medication_review", "medication_list", "chart_brief")


# --- the profiles ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["clinic-epic", "clinic-oracle", "clinic-meditech"])
def test_the_fhir_profiles_offer_the_record_tools_to_outside_assistants_read_only(name):
    p = load_profile(name)
    assert p.outside is not None and p.outside.agent == "outside" and p.agents["outside"] == RECORD_TOOLS
    assert all(p.tools[t].kind == "read" and p.tools[t].role_filtered for t in RECORD_TOOLS)
    assert (p.outside.host, p.outside.port, p.outside.mask_results) == ("127.0.0.1", 8110, "external")
    assert p.outside.secret_env == "GATEWAY_INTROSPECT_SECRET"
    text = (p.path / "outside.yaml").read_text()
    assert "secret:" not in text.replace("secret_env:", "")                       # the name of a variable, never a secret
    # an outside caller is still held to its role: the front desk may look up a record, nothing more
    assert set(p.roles["front_desk"].tools) & set(RECORD_TOOLS) == {"search_records"}
    assert p.sensitivity is not None and p.roles["front_desk"].sensitive_categories == ()
    # record data never goes to a client at an outside AI service
    assert all(p.tools[t].data_label in p.routing["never_external_labels"] for t in RECORD_TOOLS)


def test_the_start_script_is_valid_shell_and_runs_the_gateway_without_a_broker():
    script = ROOT / "scripts" / "gateway.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    assert script.stat().st_mode & stat.S_IXUSR
    text = script.read_text()
    assert "export AUDIT_SINK=file" in text and "docker compose up -d opa" in text
    for server in ("mcp_servers.fhir_records.server", "mcp_servers.medication_review.server",
                   "mcp_servers.chart_brief.server", "gateway.endpoint"):
        assert server in text
    assert "broker" not in text and "ollama" not in text.lower()


# --- the development identity stand-in ---------------------------------------------------------------------------

def test_a_token_is_shown_once_and_only_its_hash_is_kept(tmp_path):
    path = tmp_path / "config" / "clients.json"
    token = dev_identity.add_client(path, "My assistant", "clinician")
    stored = path.read_text()
    assert token not in stored and dev_identity.digest(token) in stored and len(token) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(ValueError, match="exists"):
        dev_identity.add_client(path, "My assistant", "nurse")
    with pytest.raises(ValueError, match="location"):
        dev_identity.add_client(path, "Other", "nurse", location="moon")
    assert dev_identity.remove_client(path, "My assistant") == 1 and dev_identity.read_clients(path) == {}


def test_a_check_needs_the_shared_secret_and_an_unknown_token_is_inactive(tmp_path):
    path = tmp_path / "clients.json"
    token = dev_identity.add_client(path, "Front desk app", "front_desk", location="external")
    clients = dev_identity.read_clients(path)
    body = json.dumps({"token": token}).encode()
    assert dev_identity.answer(clients, "s3cret", None, body) == (401, {"error": "unauthorized"})
    assert dev_identity.answer(clients, "s3cret", "wrong", body)[0] == 401
    assert dev_identity.answer(clients, "s3cret", "s3cret", b'{"token": "guess"}') == (200, {"active": False})
    assert dev_identity.answer(clients, "s3cret", "s3cret", b"not json") == (200, {"active": False})
    status, reply = dev_identity.answer(clients, "s3cret", "s3cret", body)
    assert status == 200 and reply["active"] is True
    assert (reply["name"], reply["role"], reply["location"]) == ("Front desk app", "front_desk", "external")
    assert token not in json.dumps(reply)


def test_the_gateways_own_token_check_reads_the_stand_ins_answer(tmp_path):
    path = tmp_path / "clients.json"
    token = dev_identity.add_client(path, "Ward assistant", "nurse")

    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    def post(url, json=None, headers=None, timeout=None):                 # the HTTP call, answered in process
        import json as j
        return Reply(*dev_identity.answer(dev_identity.read_clients(path), "s3cret",
                                          headers.get("X-Introspect-Secret"), j.dumps(json).encode()))
    who = Introspector("http://127.0.0.1:8120/introspect", "s3cret", 30, post=post).identify(token)
    assert (who.name, who.role, who.location) == ("Ward assistant", "nurse", "local")
    assert Introspector("http://x", "s3cret", 30, post=post).identify("not-a-token") is None
    assert Introspector("http://x", "other-secret", 30, post=post).identify(token) is None


def test_serving_refuses_to_start_without_the_shared_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GATEWAY_INTROSPECT_SECRET", raising=False)
    assert dev_identity.main(["--file", str(tmp_path / "c.json"), "serve"]) == 2
    assert "Set GATEWAY_INTROSPECT_SECRET" in capsys.readouterr().out
    assert dev_identity.HOST == "127.0.0.1"                                # loopback only, with no option to change it


# --- the check client -----------------------------------------------------------------------------------------------

def test_the_check_client_prints_ids_and_counts_and_text_only_when_asked(monkeypatch):
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("gateway_check", ROOT / "scripts" / "gateway_check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    result = {"results": [{"id": "med-1", "title": "Current medication", "text": "Ann Lee: lisinopril",
                           "sensitivity": []},
                          {"id": "med-2", "title": "Current medication", "text": "Ann Lee: sertraline",
                           "sensitivity": ["mental_health"]}],
              "withheld_sensitive": 1, "answer_withheld": True}
    plain = "\n".join(check.summary(result, show_text=False))
    assert "records returned: 2" in plain and "records withheld for this role (protected categories): 1" in plain
    assert "med-2: Current medication [mental_health]" in plain and "Ann Lee" not in plain
    assert "Ann Lee: sertraline" in "\n".join(check.summary(result, show_text=True))
    from ops import dev_identity

    def no_token(role):
        raise ValueError("no stand-in here")
    monkeypatch.setattr(dev_identity, "saved_token", no_token)                 # never touch a developer's real files
    assert check.main([]) == 2                                              # no token, and none can be made: says so
    used = []
    monkeypatch.setattr(dev_identity, "saved_token", lambda role: used.append(role) or "saved-token")
    monkeypatch.setattr(check, "run", lambda url, token, tool, query, show: used.append(token) or _done())
    assert check.main(["--as", "front_desk"]) == 0 and used == ["front_desk", "saved-token"]
    monkeypatch.setenv("GATEWAY_TOKEN", "mine")                                # a token of your own wins over --as
    assert check.main(["--as", "front_desk"]) == 0 and used[-1] == "mine" and len(used) == 3
    # a refusal, a stopped request and an unknown shape are said as what they are, never as "0 records"
    refused = check.summary({"error": "tool_failed", "message": "The tool could not run (ConnectError)."}, False)
    assert refused == ["the gateway answered: tool_failed: The tool could not run (ConnectError)."]
    stopped = check.summary({"stopped": "decision", "category": "medical_decision", "reply": "A clinician decides."}, False)
    assert "stopped by the checks (decision: medical_decision)" in stopped[0] and stopped[1] == "A clinician decides."
    assert "does not know, with: allowed" in check.summary({"allowed": True}, False)[0]
    assert check.summary({"results": []}, False) == ["records returned: 0"]


def test_the_stand_in_and_the_profiles_agree_on_a_port_no_other_service_here_uses(tmp_path, monkeypatch, capsys):
    # 8300 belongs to another program on a developer's machine; the first trial of the gateway stopped on it.
    assert dev_identity.PORT == 8120
    for name in ("clinic-epic", "clinic-oracle", "clinic-meditech"):
        assert load_profile(name).outside.introspect_url == "http://127.0.0.1:8120/introspect"
    assert "--port $IDENTITY_PORT|$IDENTITY_PORT" in (ROOT / "scripts" / "gateway.sh").read_text()

    def busy(address, handler):
        raise OSError(48, "Address already in use")
    monkeypatch.setattr(dev_identity, "ThreadingHTTPServer", busy)
    monkeypatch.setenv("GATEWAY_INTROSPECT_SECRET", "s3cret")
    assert dev_identity.main(["--file", str(tmp_path / "c.json"), "serve", "--port", "8121"]) == 1
    said = capsys.readouterr().out
    assert "cannot listen on 127.0.0.1:8121" in said and "outside.yaml auth.url" in said


async def _done() -> int:
    return 0


# ---- saved development tokens: no copying a token by hand

def test_a_saved_token_is_made_once_reused_and_known_to_the_stand_in(tmp_path):
    from ops import dev_identity as di
    clients = tmp_path / "clients.json"
    first = di.saved_token("clinician", clients_file=clients)
    assert di.saved_token("clinician", clients_file=clients) == first              # reused, not made again
    assert len(di.read_clients(clients)) == 1
    status, body = di.answer(di.read_clients(clients), "s3cret", "s3cret", ('{"token": "%s"}' % first).encode())
    assert status == 200 and body["active"] and body["role"] == "clinician" and body["name"] == "dev-clinician"
    desk = di.saved_token("front_desk", clients_file=clients)
    assert desk != first and {c["role"] for c in di.read_clients(clients).values()} == {"clinician", "front_desk"}
    tokens = clients.with_name(di.TOKENS_FILE.name)
    assert oct(tokens.stat().st_mode)[-3:] == "600"                                # readable by its owner only
    assert first not in clients.read_text()                                        # the clients file still holds hashes only


def test_a_saved_token_is_replaced_when_its_caller_is_gone_or_its_role_changed(tmp_path):
    from ops import dev_identity as di
    clients = tmp_path / "clients.json"
    first = di.saved_token("clinician", clients_file=clients)
    di.remove_client(clients, "dev-clinician")
    second = di.saved_token("clinician", clients_file=clients)
    assert second != first and len(di.read_clients(clients)) == 1
    third = di.saved_token("nurse", name="dev-clinician", clients_file=clients)    # same name, another role
    assert third != second and [c["role"] for c in di.read_clients(clients).values()] == ["nurse"]


def test_saved_tokens_are_refused_in_production(tmp_path, monkeypatch, capsys):
    from ops import dev_identity as di
    monkeypatch.setenv("GOVERNED_ENV", "production")
    with pytest.raises(ValueError, match="production"):
        di.saved_token("clinician", clients_file=tmp_path / "c.json")
    assert di.main(["--file", str(tmp_path / "c.json"), "token", "--role", "clinician"]) == 2
    assert not (tmp_path / "c.json").exists()


def test_the_token_command_prints_only_the_token(tmp_path, capsys):
    from ops import dev_identity as di
    assert di.main(["--file", str(tmp_path / "c.json"), "token", "--role", "clinician"]) == 0
    out = capsys.readouterr().out.strip()
    assert "\n" not in out and di.digest(out) in di.read_clients(tmp_path / "c.json")
