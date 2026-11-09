"""Cloud deployment pieces (ADR 031): the tool-server launcher, settings a deployment changes, the
image's exclusions, and the Terraform's private-only promises. `tofu validate` runs separately
(deploy/README.md); these tests catch a promise broken in a later edit."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ops import run_all

ROOT = Path(__file__).resolve().parent.parent
TF = ROOT / "deploy" / "terraform"


def _tf(cloud: str) -> str:
    """The cloud's Terraform with runs of spaces collapsed (tofu fmt aligns the equals signs)."""
    return re.sub(r"[ \t]+", " ", "\n".join(f.read_text() for f in sorted((TF / cloud).glob("*.tf"))))


# --- ops.run_all ----------------------------------------------------------------------------------

def test_run_all_starts_the_requested_tool_servers_each_under_a_supervisor():
    assert run_all.module("drug-reference") == "mcp_servers.drug_reference.server"
    assert run_all.wanted([], env={"TOOL_SERVERS": "records, changes,records"}) == ["records", "changes"]
    assert run_all.wanted([], env={}) == run_all.ALL
    with pytest.raises(SystemExit, match="unknown tool server"):
        run_all.module("shell")


def test_every_tool_server_in_the_launcher_exists():
    for name in run_all.ALL:
        assert (ROOT / "mcp_servers" / name.replace("-", "_") / "server.py").is_file(), name


def test_run_all_stops_the_others_when_one_supervisor_gives_up():
    started = []

    class Child:
        def __init__(self, argv):
            self.argv, self.code, self.stopped = argv, None, False
            started.append(self)

        def poll(self):
            return self.code

        def terminate(self):
            self.stopped = True
            self.code = self.code if self.code is not None else -15

        def wait(self):
            return self.code

    def sleep(_):
        started[0].code = 3                          # the first supervisor gave up
    assert run_all.main(["records", "changes"], popen=Child, sleep=sleep) == 3
    assert started[0].argv[-3:] == ["records", "--", "mcp_servers.records.server"]
    assert started[1].stopped


# --- settings a deployment changes -----------------------------------------------------------------

def _fresh(code: str, **env) -> str:
    """Run code in a new Python process, so module-level settings are read again without reloading
    modules that other tests hold."""
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), **env})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_ollama_can_run_on_another_host():
    code = "import llm.providers as p, guardrails.classifier as g; print(p.OLLAMA_URL, g.OLLAMA_GENERATE_URL)"
    assert _fresh(code, OLLAMA_HOST="10.50.4.4:11434") == \
        "http://10.50.4.4:11434/v1/chat/completions http://10.50.4.4:11434/api/generate"
    assert _fresh(code, OLLAMA_HOST="").startswith("http://localhost:11434/v1/chat/completions")


def test_the_producer_is_idempotent_unless_switched_off():
    code = "import ops.delivery as d; print(d.IDEMPOTENT)"
    assert _fresh(code) == "True"
    assert _fresh(code, KAFKA_IDEMPOTENCE="0") == "False"


# --- the image ------------------------------------------------------------------------------------

def test_the_image_never_contains_secrets_or_local_state():
    ignored = (ROOT / ".dockerignore").read_text().split()
    for item in (".env", ".kafka.env", ".keycloak.env", "certs/", "logs/", "*.db", "audit.jsonl", ".git/"):
        assert item in ignored, item
    docker = (ROOT / "Dockerfile").read_text()
    assert "USER governed" in docker and "--group deploy" in docker
    assert "sample_csv" in docker                    # synthetic data only


# --- Terraform: private only ----------------------------------------------------------------------

@pytest.mark.parametrize("cloud", ["azure", "aws"])
def test_both_clouds_take_kafka_access_from_the_one_access_file(cloud):
    assert 'yamldecode(file("${path.module}/../../../ops/kafka_access.yaml"))' in _tf(cloud)


def test_azure_has_no_public_endpoint():
    tf = _tf("azure")
    assert "local_authentication_enabled = false" in tf            # Event Hubs: Entra ID only, no keys
    assert tf.count("public_network_access_enabled = false") >= 3   # Event Hubs and both storage accounts
    assert "internal_load_balancer_enabled = true" in tf        # the apps have a private address only
    assert "azurerm_public_ip" in tf and 'count = var.internet_egress ? 1 : 0' in tf  # NAT only
    assert "public_ip_address_id" not in re.sub(r"azurerm_nat_gateway_public_ip_association[\s\S]*?\n}", "", tf)
    assert tf.count("default_outbound_access_enabled = false") == 3
    assert 'KAFKA_OAUTH_PROVIDER = "azure"' in tf


def test_aws_has_no_public_endpoint():
    tf = _tf("aws")
    assert "unauthenticated = false" in tf and "iam = true" in tf and 'client_broker = "TLS"' in tf
    assert 'type = "DISABLED"' in tf                                # MSK public access
    assert "internal = true" in tf                # the load balancer
    assert tf.count("assign_public_ip = false") == 1 and "associate_public_ip_address = false" in tf
    assert 'http_tokens = "required"' in tf
    assert "map_public_ip_on_launch" not in tf
    assert 'KAFKA_OAUTH_PROVIDER = "aws-msk-iam"' in tf


def test_terraform_state_and_variables_are_never_committed():
    ignore = (ROOT / ".gitignore").read_text()
    assert "deploy/terraform/**/*.tfstate*" in ignore and "deploy/terraform/**/*.tfvars" in ignore
