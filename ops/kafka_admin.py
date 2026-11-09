"""Topics and least-privilege access from ops/kafka_access.yaml (audit SC-3, ADR 031).

  python -m ops.kafka_admin acl-commands     kafka-acls arguments, one line each, for the secured
                                             local broker (scripts/kafka_secure.sh runs them inside it)
  python -m ops.kafka_admin create-topics    create the topics (retention from the access file) on the
                                             broker that KAFKA_* points to, as KAFKA_USERNAME_ADMIN or
                                             an admin IAM role (the AWS setup task, deploy/modules/aws/apps)
  python -m ops.kafka_admin apply-acls       write the same ACLs through the Kafka admin API (any
                                             SASL/SCRAM cluster where this process is a super user)
  python -m ops.kafka_admin selfcheck        against the secured broker: a wrong password, plaintext and a
                                             forbidden write are refused, a permitted sign-in works
  python -m ops.kafka_admin check            print each service's access and exit

Writing to a topic implies describing it; reading needs read on the topic and on the consumer group.
Nothing else is granted: no topic creation, no deletion, no cluster changes.
"""

import os
import sys
from pathlib import Path

import yaml

ACCESS_FILE = Path(__file__).with_name("kafka_access.yaml")
FOREVER = "-1"


def _flat(items) -> list[str]:
    out: list[str] = []
    for i in items or []:
        out.extend(_flat(i) if isinstance(i, list) else [str(i)])
    return out


def load_access(path: Path = ACCESS_FILE) -> dict:
    raw = yaml.safe_load(path.read_text())
    topics = {name: (spec or {}).get("keep", "default") for name, spec in raw["topics"].items()}
    services = {}
    for name, spec in raw["services"].items():
        spec = spec or {}
        services[name] = {k: _flat(spec.get(k)) for k in ("write", "read", "groups")}
        unknown = [t for t in services[name]["write"] + services[name]["read"] if t not in topics]
        if unknown:
            raise ValueError(f"{name} uses topics not listed under topics: {', '.join(unknown)}")
    return {"topics": topics, "services": services}


def bindings(access: dict) -> list[tuple[str, str, str, str, str]]:
    """(principal, resource type, name, pattern type, operation) for every grant."""
    out = []
    for svc, spec in access["services"].items():
        p = f"User:{svc}"
        out += [(p, "topic", t, "literal", "Write") for t in spec["write"]]
        out += [(p, "topic", t, "literal", "Read") for t in spec["read"]]
        out += [(p, "group", g, "prefixed" if g.endswith("-") else "literal", "Read") for g in spec["groups"]]
    return out


def local_ui_bindings() -> list[tuple[str, str, str, str, str]]:
    """Kafka UI on the secured local broker: it may look, never write or change anything."""
    p = "User:kafka-ui"
    return [(p, "topic", "*", "literal", op) for op in ("Read", "Describe", "DescribeConfigs")] + \
           [(p, "group", "*", "literal", "Describe"), (p, "cluster", "kafka-cluster", "literal", "Describe"),
            (p, "cluster", "kafka-cluster", "literal", "DescribeConfigs")]


def acl_commands(access: dict, bootstrap: str = "localhost:29094") -> list[str]:
    """kafka-acls arguments, grouped so each principal needs only a few calls (each one starts a JVM)."""
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for principal, kind, name, pattern, op in bindings(access) + local_ui_bindings():
        target = "--cluster" if kind == "cluster" else f"--{kind} {name}"
        grouped.setdefault((principal, pattern, op), [])
        if target not in grouped[(principal, pattern, op)]:
            grouped[(principal, pattern, op)].append(target)
    return [f"--bootstrap-server {bootstrap} --add --allow-principal {principal} --operation {op} "
            f"{' '.join(targets)} --resource-pattern-type {pattern}"
            for (principal, pattern, op), targets in grouped.items()]


def topic_configs(access: dict) -> dict[str, dict[str, str]]:
    return {t: ({"retention.ms": FOREVER} if keep == "forever" else {}) for t, keep in access["topics"].items()}


def create_topics(access: dict) -> int:  # pragma: no cover - needs a broker
    from confluent_kafka.admin import AdminClient, NewTopic

    from ops.kafka import describe, kafka_config
    conf = kafka_config("admin")
    print(describe(conf))
    admin = AdminClient(conf)
    partitions = int(os.environ.get("KAFKA_PARTITIONS", "3"))
    replication = int(os.environ.get("KAFKA_REPLICATION", "3"))
    existing = set(admin.list_topics(timeout=15).topics)
    new = [NewTopic(t, num_partitions=partitions, replication_factor=replication, config=c)
           for t, c in topic_configs(access).items() if t not in existing]
    failed = 0
    for topic, future in (admin.create_topics(new).items() if new else []):
        try:
            future.result()
            print(f"created {topic}")
        except Exception as e:
            failed += 1
            print(f"could not create {topic}: {e}")
    print(f"{len(existing & set(access['topics']))} topics already there")
    return 1 if failed else 0


def apply_acls(access: dict) -> int:  # pragma: no cover - needs a broker
    from confluent_kafka.admin import AclBinding, AclOperation, AclPermissionType, AdminClient, ResourcePatternType, ResourceType

    from ops.kafka import kafka_config
    admin = AdminClient(kafka_config("admin"))
    acls = [AclBinding(ResourceType[kind.upper()], name, ResourcePatternType[pattern.upper()], principal, "*",
                       AclOperation[op.upper().replace("CONFIGS", "_CONFIGS")], AclPermissionType.ALLOW)
            for principal, kind, name, pattern, op in bindings(access)]
    failed = 0
    for acl, future in admin.create_acls(acls).items():
        try:
            future.result()
        except Exception as e:
            failed += 1
            print(f"could not add {acl}: {e}")
    print(f"{len(acls) - failed} of {len(acls)} ACLs in place")
    return 1 if failed else 0


def selfcheck() -> int:  # pragma: no cover - needs the secured broker (scripts/kafka_secure.sh check)
    """Against the secured broker: what must work works, and what must be refused is refused.
    Each result is printed as soon as it is known; the client's own error lines are hidden, since
    three of the four tests are meant to fail."""
    import logging

    from confluent_kafka import KafkaException, Producer

    from ops.kafka import kafka_config
    quiet = logging.getLogger("kafka-selfcheck")
    quiet.addHandler(logging.NullHandler())
    quiet.propagate = False

    def client(conf: dict) -> Producer:
        return Producer({**conf, "logger": quiet, "message.timeout.ms": 10000})

    def connects(conf: dict) -> bool:
        """Signed in and allowed to see the audit topics (an ACL), not just a TCP connection."""
        try:
            return "audit.tool_calls" in client(conf).list_topics(timeout=8).topics
        except KafkaException:
            return False

    results = []

    def report(what: str, ok: bool) -> None:
        results.append(ok)
        print(f"  {'ok  ' if ok else 'FAIL'} {what}", flush=True)

    print("Checking the secured Kafka (about 30 seconds)...", flush=True)
    report("audit-verify signs in over TLS", connects(kafka_config("audit-verify")))
    report("a wrong password is refused",
           not connects({**kafka_config("audit-verify"), "sasl.password": "wrong-password"}))
    plain = kafka_config("audit-verify", env={"KAFKA_BOOTSTRAP_SERVERS": os.environ.get("KAFKA_BOOTSTRAP_SERVERS",
                                                                                         "127.0.0.1:9092")})
    report("plaintext is refused", not connects(plain))
    outcome = {}
    p = client(kafka_config("audit-verify"))          # the verifier may read the audit chain, never write it
    p.produce("audit.tool_calls", value=b"{}", on_delivery=lambda err, msg: outcome.setdefault("err", err))
    p.flush(20)
    err = outcome.get("err")
    report("audit-verify cannot write to the audit chain",
           err is not None and "AUTHORIZATION" in str(err.name() if hasattr(err, "name") else err).upper())
    return 0 if all(results) else 1


def main(argv: list[str]) -> int:
    access = load_access()
    cmd = argv[0] if argv else "check"
    if cmd == "acl-commands":
        print("\n".join(acl_commands(access, argv[1] if len(argv) > 1 else "localhost:29094")))
    elif cmd == "create-topics":
        return create_topics(access)
    elif cmd == "apply-acls":
        return apply_acls(access)
    elif cmd == "selfcheck":
        return selfcheck()
    elif cmd == "check":
        for svc, spec in access["services"].items():
            print(f"{svc}: writes {', '.join(spec['write']) or 'nothing'}; reads {', '.join(spec['read']) or 'nothing'}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
