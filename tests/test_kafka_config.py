"""Kafka client settings (audit SC-3, ADR 031): local plaintext by default, TLS and SASL from the
environment, a separate identity per service, Entra ID and MSK IAM tokens, and no plaintext to a
broker on another machine."""

import sys
import types

import pytest

from ops.kafka import KafkaConfigError, describe, kafka_config

MSK = "b-1.governed.abc123.c2.kafka.us-east-1.amazonaws.com:9096,b-2.governed.abc123.c2.kafka.us-east-1.amazonaws.com:9096"
EVENT_HUBS = "governed-prod.servicebus.windows.net:9093"


def test_the_default_is_the_local_broker_without_tls():
    c = kafka_config("planner", env={})
    assert c["bootstrap.servers"] == "127.0.0.1:9092" and c["security.protocol"] == "PLAINTEXT"
    assert c["client.id"] == "governed-planner"


@pytest.mark.parametrize("protocol", ["PLAINTEXT", "SASL_PLAINTEXT"])
def test_plaintext_to_another_machine_is_refused(protocol):
    with pytest.raises(KafkaConfigError, match="not on this machine"):
        kafka_config("api", env={"KAFKA_BOOTSTRAP_SERVERS": MSK, "KAFKA_SECURITY_PROTOCOL": protocol,
                                 "KAFKA_USERNAME": "u", "KAFKA_PASSWORD": "p"})


def test_plaintext_elsewhere_only_on_purpose():
    c = kafka_config("api", env={"KAFKA_BOOTSTRAP_SERVERS": "10.0.0.5:9092", "KAFKA_ALLOW_INSECURE": "1"})
    assert c["security.protocol"] == "PLAINTEXT"


def test_scram_over_tls_with_a_separate_identity_per_service():
    env = {"KAFKA_BOOTSTRAP_SERVERS": MSK, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
           "KAFKA_USERNAME_PLANNER": "planner", "KAFKA_PASSWORD_PLANNER": "p1",
           "KAFKA_USERNAME": "shared", "KAFKA_PASSWORD": "p0", "KAFKA_CA_FILE": "/etc/ca.pem"}
    p = kafka_config("planner", env=env)
    assert (p["sasl.mechanisms"], p["sasl.username"], p["sasl.password"]) == ("SCRAM-SHA-512", "planner", "p1")
    assert p["ssl.ca.location"] == "/etc/ca.pem" and p["ssl.endpoint.identification.algorithm"] == "https"
    e = kafka_config("executor", env=env)                    # no executor identity: the shared one
    assert e["sasl.username"] == "shared"


def test_a_password_can_come_from_a_mounted_secret_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("from-file\n")
    c = kafka_config("audit-verify", env={"KAFKA_BOOTSTRAP_SERVERS": MSK, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                                          "KAFKA_USERNAME_AUDIT_VERIFY": "auditor",
                                          "KAFKA_PASSWORD_AUDIT_VERIFY_FILE": str(f)})
    assert c["sasl.password"] == "from-file"
    with pytest.raises(KafkaConfigError, match="cannot be read"):
        kafka_config("api", env={"KAFKA_SECURITY_PROTOCOL": "SASL_SSL", "KAFKA_USERNAME": "u",
                                 "KAFKA_PASSWORD_FILE": str(tmp_path / "missing")})


def test_sasl_without_credentials_says_what_is_missing():
    with pytest.raises(KafkaConfigError, match="KAFKA_USERNAME_API"):
        kafka_config("api", env={"KAFKA_SECURITY_PROTOCOL": "SASL_SSL"})


def test_event_hubs_with_a_connection_string():
    c = kafka_config("api", env={"KAFKA_BOOTSTRAP_SERVERS": EVENT_HUBS, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                                 "KAFKA_SASL_MECHANISM": "PLAIN", "KAFKA_USERNAME": "$ConnectionString",
                                 "KAFKA_PASSWORD": "Endpoint=sb://governed-prod.servicebus.windows.net/;..."})
    assert c["sasl.mechanisms"] == "PLAIN" and c["sasl.username"] == "$ConnectionString"


def test_client_certificates_go_together():
    base = {"KAFKA_SECURITY_PROTOCOL": "SSL"}
    c = kafka_config("api", env={**base, "KAFKA_CLIENT_CERT_FILE": "c.pem", "KAFKA_CLIENT_KEY_FILE": "k.pem"})
    assert c["ssl.certificate.location"] == "c.pem" and c["ssl.key.location"] == "k.pem"
    with pytest.raises(KafkaConfigError, match="both"):
        kafka_config("api", env={**base, "KAFKA_CLIENT_CERT_FILE": "c.pem"})


def test_unknown_settings_are_refused():
    with pytest.raises(KafkaConfigError):
        kafka_config("api", env={"KAFKA_SECURITY_PROTOCOL": "TLS"})
    with pytest.raises(KafkaConfigError):
        kafka_config("api", env={"KAFKA_SECURITY_PROTOCOL": "SASL_SSL", "KAFKA_SASL_MECHANISM": "GSSAPI"})


def test_entra_id_tokens_for_event_hubs(monkeypatch):
    seen = {}

    class Credential:
        def get_token(self, scope):
            seen["scope"] = scope
            return types.SimpleNamespace(token="entra-token", expires_on=1_900_000_000)
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", types.SimpleNamespace(DefaultAzureCredential=Credential))
    c = kafka_config("planner", env={"KAFKA_BOOTSTRAP_SERVERS": EVENT_HUBS, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                                     "KAFKA_SASL_MECHANISM": "OAUTHBEARER", "KAFKA_OAUTH_PROVIDER": "azure"})
    assert c["sasl.mechanisms"] == "OAUTHBEARER"
    assert c["oauth_cb"]("") == ("entra-token", 1_900_000_000.0)
    assert seen["scope"] == "https://governed-prod.servicebus.windows.net/.default"
    with pytest.raises(KafkaConfigError, match="servicebus"):
        kafka_config("planner", env={"KAFKA_BOOTSTRAP_SERVERS": MSK, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                                     "KAFKA_SASL_MECHANISM": "OAUTHBEARER", "KAFKA_OAUTH_PROVIDER": "azure"})


def test_msk_iam_tokens(monkeypatch):
    signer = types.SimpleNamespace(MSKAuthTokenProvider=types.SimpleNamespace(
        generate_auth_token=lambda region: (f"msk-token-{region}", 1_900_000_000_000)))
    monkeypatch.setitem(sys.modules, "aws_msk_iam_sasl_signer", signer)
    env = {"KAFKA_BOOTSTRAP_SERVERS": MSK, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL", "KAFKA_SASL_MECHANISM": "OAUTHBEARER",
           "KAFKA_OAUTH_PROVIDER": "aws-msk-iam", "AWS_REGION": "us-east-1"}
    assert kafka_config("api", env=env)["oauth_cb"]("") == ("msk-token-us-east-1", 1_900_000_000.0)
    with pytest.raises(KafkaConfigError, match="AWS_REGION"):
        kafka_config("api", env={k: v for k, v in env.items() if k != "AWS_REGION"})
    with pytest.raises(KafkaConfigError, match="KAFKA_OAUTH_PROVIDER"):
        kafka_config("api", env={**env, "KAFKA_OAUTH_PROVIDER": ""})


def test_startup_logs_never_show_a_secret():
    c = kafka_config("api", env={"KAFKA_BOOTSTRAP_SERVERS": MSK, "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                                 "KAFKA_USERNAME": "api", "KAFKA_PASSWORD": "hunter2-secret"})
    text = describe(c)
    assert "SASL_SSL SCRAM-SHA-512 as api" in text and "hunter2" not in text


def test_the_gateway_uses_the_endpoints_identity_and_extra_settings_merge():
    env = {"KAFKA_SECURITY_PROTOCOL": "SASL_SSL", "KAFKA_USERNAME_ENDPOINT": "endpoint", "KAFKA_PASSWORD_ENDPOINT": "x"}
    c = kafka_config("gateway", {"group.id": "g", "acks": "all"}, env=env)
    assert c["sasl.username"] == "endpoint" and c["group.id"] == "g" and c["acks"] == "all"
