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


# --- Terraform modules and Terragrunt units ------------------------------------------------------

DEPLOY = ROOT / "deploy"
MODULES = DEPLOY / "modules"
LIVE = DEPLOY / "live"
CLOUDS = ["azure", "aws"]


def _tf(path: Path) -> str:
    """Terraform text with runs of spaces collapsed (tofu fmt aligns the equals signs)."""
    return re.sub(r"[ \t]+", " ", "\n".join(f.read_text() for f in sorted(path.glob("*.tf"))))


def _blocks(text: str, kind: str) -> dict[str, str]:
    """Top-level `kind "name" { ... }` blocks by name (variable, output)."""
    out, pattern = {}, re.compile(rf'^{kind} "([^"]+)" \{{', re.M)
    for m in pattern.finditer(text):
        depth, i = 0, m.end() - 1
        for j in range(i, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                out[m.group(1)] = text[i:j + 1]
                break
    return out


def _variables(module: Path) -> dict[str, bool]:
    """Variable name -> required (no default)."""
    return {n: "default" not in body for n, body in _blocks(_tf(module), "variable").items()}


def _outputs(module: Path) -> set[str]:
    return set(_blocks(_tf(module), "output"))


def _inputs(hcl: str) -> set[str]:
    m = re.search(r"^inputs = \{(.*?)^\}", hcl, re.M | re.S)
    return set(re.findall(r"^  ([a-z_]+)\s*=", m.group(1), re.M)) if m else set()


def _units(cloud: str) -> list[str]:
    return sorted(f.stem for f in (LIVE / cloud / "_common").glob("*.hcl") if f.stem != "mocks")


@pytest.mark.parametrize("cloud", CLOUDS)
def test_every_unit_has_a_module_and_every_environment_has_every_unit(cloud):
    units = _units(cloud)
    for u in units:
        assert (MODULES / cloud / u / "main.tf").is_file(), u
    envs = sorted(d.name for d in (LIVE / cloud).iterdir() if (d / "env.hcl").is_file())
    assert {"dev", "prod"} <= set(envs)
    for e in envs:
        assert sorted(d.name for d in (LIVE / cloud / e).iterdir() if d.is_dir()) == units, e
        for u in units:
            unit = (LIVE / cloud / e / u / "terragrunt.hcl").read_text()
            assert 'find_in_parent_folders("root.hcl")' in unit and "_common/" in unit, (e, u)


@pytest.mark.parametrize("cloud", CLOUDS)
def test_every_input_is_a_module_variable_and_every_required_variable_is_given(cloud):
    root = (LIVE / cloud / "root.hcl").read_text()
    shared = _inputs(root)
    for u in _units(cloud):
        variables = _variables(MODULES / cloud / u)
        given = _inputs((LIVE / cloud / "_common" / f"{u}.hcl").read_text())
        assert given <= set(variables), (u, sorted(given - set(variables)))     # a misspelled input
        missing = {v for v, required in variables.items() if required} - given - shared
        assert not missing, (u, sorted(missing))


@pytest.mark.parametrize("cloud", CLOUDS)
def test_every_dependency_output_used_exists_in_the_module_and_in_the_mocks(cloud):
    mocks = (LIVE / cloud / "_common" / "mocks.hcl").read_text()
    for u in _units(cloud):
        hcl = (LIVE / cloud / "_common" / f"{u}.hcl").read_text()
        for dep, out in set(re.findall(r"dependency\.([a-z]+)\.outputs\.([a-z_]+)", hcl)):
            assert out in _outputs(MODULES / cloud / dep), (u, dep, out)
            block = re.search(rf"^  {dep} = \{{(.*?)^  \}}|^  {dep}\s*= \{{([^\n]*)\}}", mocks, re.M | re.S)
            assert block and re.search(rf"\b{out}\b", block.group(0)), (u, dep, out, "mock")


@pytest.mark.parametrize("cloud", CLOUDS)
def test_the_kafka_access_list_reaches_the_modules_from_the_one_file(cloud):
    root = (LIVE / cloud / "root.hcl").read_text()
    assert 'yamldecode(file("${get_parent_terragrunt_dir()}/../../../ops/kafka_access.yaml"))' in root
    assert re.search(r"^\s*kafka_access\s*=\s*local\.kafka_access$", root, re.M)
    assert "kafka_access" in _variables(MODULES / cloud / "kafka")


def test_environments_are_kept_apart():
    for cloud, key in (("azure", "address_space"), ("aws", "vpc_cidr")):
        ranges = [re.search(rf'{key}\s*=\s*"([^"]+)"', (LIVE / cloud / e / "env.hcl").read_text()).group(1)
                  for e in ("dev", "prod")]
        assert len(set(ranges)) == 2, cloud                                  # so they can be peered later
    aws_root = (LIVE / "aws" / "root.hcl").read_text()
    assert "allowed_account_ids" in aws_root                               # dev never applied to prod
    for cloud in CLOUDS:
        root = (LIVE / cloud / "root.hcl").read_text()
        assert "path_relative_to_include()}/terraform.tfstate" in root       # one state per unit
        assert 'GOVERNED_TG_LOCAL_STATE", "") == "1"' in root                 # local state only on request


def test_modules_have_no_provider_blocks_or_paths_outside_deploy_modules():
    for f in MODULES.rglob("*.tf"):
        text = f.read_text()
        assert not re.search(r'^provider "', text, re.M), f                  # providers come from root.hcl
        for ref in re.findall(r'"\$\{path\.module\}/([^"]+)"', text) + re.findall(r'source\s*=\s*"(\.\./[^"]+)"', text):
            assert (f.parent / ref).resolve().is_relative_to(MODULES.resolve()), (f, ref)


def test_azure_has_no_public_endpoint():
    tf = "\n".join(_tf(d) for d in (MODULES / "azure").iterdir() if d.is_dir())
    assert "local_authentication_enabled = false" in tf                    # Event Hubs: Entra ID only
    assert tf.count("public_network_access_enabled = false") >= 3          # Event Hubs and both storage accounts
    assert "internal_load_balancer_enabled = true" in tf                   # the apps have a private address only
    assert tf.count("default_outbound_access_enabled = false") == 3
    assert "public_ip_address_id" not in re.sub(r"azurerm_nat_gateway_public_ip_association[\s\S]*?\n}", "", tf)
    assert 'KAFKA_OAUTH_PROVIDER = "azure"' in tf


def test_aws_has_no_public_endpoint():
    tf = "\n".join(_tf(d) for d in (MODULES / "aws").iterdir() if d.is_dir())
    assert "unauthenticated = false" in tf and "iam = true" in tf and 'client_broker = "TLS"' in tf
    assert 'type = "DISABLED"' in tf                                       # MSK public access
    assert "internal = true" in tf                                         # the load balancer
    assert tf.count("assign_public_ip = false") == 1 and "associate_public_ip_address = false" in tf
    assert 'http_tokens = "required"' in tf and "map_public_ip_on_launch" not in tf
    assert 'KAFKA_OAUTH_PROVIDER = "aws-msk-iam"' in tf


def test_terraform_state_caches_and_variables_are_never_committed_or_in_the_image():
    ignore = (ROOT / ".gitignore").read_text()
    for item in ("deploy/**/.terragrunt-cache/", "deploy/**/.local-state/", "deploy/**/*.tfstate*", "deploy/**/*.tfvars"):
        assert item in ignore, item
    assert "deploy/" in (ROOT / ".dockerignore").read_text().split()
