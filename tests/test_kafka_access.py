"""The Kafka access list (ops/kafka_access.yaml) against the code, and the secured local broker
(docker-compose.kafka-secure.yml, scripts/kafka_secure.sh): audit SC-3, ADR 031."""

import re
from pathlib import Path

import yaml

from agents.actions import APPROVAL_TOPICS
from agents.trace import TRACE_TOPIC
from api.events import EVENT_TOPICS
from audit.chain import AUDIT_TOPICS
from ops import kafka, kafka_admin

ROOT = Path(__file__).resolve().parent.parent
ACCESS = kafka_admin.load_access()
S = ACCESS["services"]


def _shell_list(name: str) -> set[str]:
    m = re.search(rf'^{name}="([^"]+)"', (ROOT / "scripts" / "dev.sh").read_text(), re.M)
    return set(m.group(1).split())


def test_every_topic_the_code_uses_is_listed_with_the_retention_dev_sh_gives_it():
    assert set(ACCESS["topics"]) == _shell_list("TOPICS")
    assert {t for t, keep in ACCESS["topics"].items() if keep == "forever"} == _shell_list("KEEP_FOREVER")
    assert set(AUDIT_TOPICS) | set(APPROVAL_TOPICS) | {TRACE_TOPIC} <= set(ACCESS["topics"])


def test_each_reader_may_read_what_it_subscribes_to():
    assert set(EVENT_TOPICS) <= set(S["api"]["read"])              # the dashboard reads everything
    assert set(APPROVAL_TOPICS) <= set(S["api"]["read"])
    assert S["planner"]["read"] == ["chat.requests"]
    assert set(S["executor"]["read"]) == set(APPROVAL_TOPICS)
    assert set(S["audit-verify"]["read"]) == set(AUDIT_TOPICS)


def test_each_writer_may_write_what_it_produces():
    every_chain = set(AUDIT_TOPICS) | {TRACE_TOPIC}                # every process writes its own audit chain
    for svc in ("planner", "executor", "endpoint"):
        assert every_chain <= set(S[svc]["write"]), svc
    assert set(S["api"]["write"]) == {"chat.requests", "approvals.decided"}
    assert {"chat.responses", "approvals.requested"} <= set(S["planner"]["write"])
    assert {"approvals.decided", "actions.executed"} <= set(S["executor"]["write"])
    assert S["audit-verify"]["write"] == []


def test_least_privilege_where_it_matters():
    assert "chat.requests" not in S["planner"]["write"]            # the planner cannot forge a person's message
    assert "approvals.decided" not in S["planner"]["write"]        # nor approve its own change
    assert "approvals.decided" not in S["endpoint"]["write"]
    assert not set(AUDIT_TOPICS) & set(S["api"]["write"])          # the API cannot write audit events
    assert not set(S["planner"]["read"]) & set(APPROVAL_TOPICS)


def test_consumer_groups_match_the_group_ids_in_the_code():
    sources = {"api": ["api/main.py", "api/events.py"], "planner": ["agents/planner.py"],
               "executor": ["agents/executor.py"], "audit-verify": ["audit/verify.py"]}
    for svc, files in sources.items():
        text = "".join((ROOT / f).read_text() for f in files)
        ids = re.findall(r'"group\.id": f?"([a-z-]+)', text)
        assert ids, svc
        for gid in ids:
            assert any(gid.startswith(g) if g.endswith("-") else gid == g for g in S[svc]["groups"]), (svc, gid)


def test_grants_are_only_read_and_write_and_kafka_ui_only_looks():
    ops = {op for *_, op in kafka_admin.bindings(ACCESS)}
    assert ops == {"Read", "Write"}
    ui = {op for *_, op in kafka_admin.local_ui_bindings()}
    assert "Write" not in ui and "Alter" not in ui and "Create" not in ui and "Delete" not in ui
    lines = kafka_admin.acl_commands(ACCESS)
    assert all("--add --allow-principal User:" in line for line in lines)
    assert any("--group executor- " in line and "prefixed" in line for line in lines)
    assert len(lines) < 20                                          # grouped: each line starts a JVM


def test_retention_forever_only_for_the_audit_and_approval_history():
    configs = kafka_admin.topic_configs(ACCESS)
    assert configs["audit.tool_calls"] == {"retention.ms": "-1"} and configs["chat.requests"] == {}


def test_every_service_in_the_access_list_has_a_kafka_identity_and_a_local_user():
    assert set(S) <= kafka.SERVICES
    users = re.search(r'^USERS="([^"]+)"', (ROOT / "scripts" / "kafka_secure.sh").read_text(), re.M).group(1).split()
    assert set(users) == set(S) | {"kafka-ui"}


class _Tags(yaml.SafeLoader):
    pass


_Tags.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_sequence(node)
                            if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node))


def test_the_secured_broker_has_no_plaintext_listener_on_the_network():
    broker = yaml.load((ROOT / "docker-compose.kafka-secure.yml").read_text(), Loader=_Tags)["services"]["broker"]
    env = broker["environment"]
    protocols = dict(p.split(":") for p in env["KAFKA_LISTENER_SECURITY_PROTOCOL_MAP"].split(","))
    for listener in env["KAFKA_LISTENERS"].split(","):
        name, address = listener.split("://")
        if protocols[name] == "PLAINTEXT":
            assert address.startswith("localhost:"), listener      # only inside the container
        else:
            assert protocols[name] == "SASL_SSL", listener
    assert env["KAFKA_ALLOW_EVERYONE_IF_NO_ACL_FOUND"] == "false"
    assert "StandardAuthorizer" in env["KAFKA_AUTHORIZER_CLASS_NAME"]
    assert env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "false"
    assert not any("SSL" in n or "SASL" in n for n in protocols)    # the image would then demand keystores


def test_dev_sh_gives_each_service_only_its_own_kafka_password():
    text = (ROOT / "scripts" / "dev.sh").read_text()
    assert 'hide+=(-u "KAFKA_PASSWORD_$u")' in text and "docker-compose.kafka-secure.yml" in text
    assert ".kafka.env" in (ROOT / ".gitignore").read_text()
