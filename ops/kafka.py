"""How every process connects to Kafka (audit SC-3, ADR 031).

One place builds the client settings for every producer and consumer, from the environment, so the
same code runs against the local broker, Azure Event Hubs (Kafka endpoint) or Amazon MSK:

  KAFKA_BOOTSTRAP_SERVERS   host:port[,host:port]               default 127.0.0.1:9092
  KAFKA_SECURITY_PROTOCOL   PLAINTEXT | SSL | SASL_SSL          default PLAINTEXT (this machine only)
  KAFKA_SASL_MECHANISM      SCRAM-SHA-512 | SCRAM-SHA-256 | PLAIN | OAUTHBEARER
  KAFKA_USERNAME, KAFKA_PASSWORD            or KAFKA_PASSWORD_FILE (a mounted secret)
  KAFKA_USERNAME_<SERVICE>, KAFKA_PASSWORD_<SERVICE>[_FILE]   per service (API, PLANNER, EXECUTOR,
                                            ENDPOINT, AUDIT_VERIFY): each gets only its own topics
  KAFKA_CA_FILE             CA that signed the brokers' certificates (default: the system's)
  KAFKA_CLIENT_CERT_FILE, KAFKA_CLIENT_KEY_FILE   mutual TLS with the brokers
  KAFKA_OAUTH_PROVIDER      azure (Entra ID: managed identity or any DefaultAzureCredential)
                            aws-msk-iam (IAM role of the task or instance)

Safety: plaintext (no TLS) is refused for any broker that is not on this machine, unless
KAFKA_ALLOW_INSECURE=1 says so on purpose. Passwords and tokens are never logged.
"""

import os
from urllib.parse import urlparse

LOCAL = {"127.0.0.1", "localhost", "::1"}
PROTOCOLS = {"PLAINTEXT", "SSL", "SASL_SSL", "SASL_PLAINTEXT"}
MECHANISMS = {"SCRAM-SHA-512", "SCRAM-SHA-256", "PLAIN", "OAUTHBEARER"}
SERVICES = {"api", "planner", "executor", "endpoint", "audit-verify", "admin"}   # ops/kafka_access.yaml


class KafkaConfigError(Exception):
    """The Kafka settings cannot work, or would not be safe (raised at startup)."""


def _env(env, name: str) -> str | None:
    v = env.get(name)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _secret(env, name: str) -> str | None:
    """A value from NAME, or from the file NAME_FILE points to (container secrets)."""
    value = _env(env, name)
    if value:
        return value
    path = _env(env, f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError as e:
            raise KafkaConfigError(f"{name}_FILE points to {path}, which cannot be read ({type(e).__name__})") from None
    return None


def _hosts(bootstrap: str) -> list[str]:
    return [urlparse(f"//{h.strip()}").hostname or "" for h in bootstrap.split(",") if h.strip()]


def bootstrap(env=None) -> str:
    env = os.environ if env is None else env
    return _env(env, "KAFKA_BOOTSTRAP_SERVERS") or "127.0.0.1:9092"


ALIASES = {"gateway": "endpoint"}   # the governed endpoint's audit chain may be opened by its gateway first


def kafka_config(service: str, extra: dict | None = None, env=None) -> dict:
    """librdkafka settings for one service's producer or consumer."""
    env = os.environ if env is None else env
    service = ALIASES.get(service, service)
    servers = bootstrap(env)
    protocol = (_env(env, "KAFKA_SECURITY_PROTOCOL") or "PLAINTEXT").upper()
    if protocol not in PROTOCOLS:
        raise KafkaConfigError(f"KAFKA_SECURITY_PROTOCOL must be one of {', '.join(sorted(PROTOCOLS))}")
    remote = [h for h in _hosts(servers) if h not in LOCAL]
    if protocol in ("PLAINTEXT", "SASL_PLAINTEXT") and remote and env.get("KAFKA_ALLOW_INSECURE") != "1":
        raise KafkaConfigError(f"Kafka at {', '.join(remote)} is not on this machine: use KAFKA_SECURITY_PROTOCOL="
                               "SASL_SSL or SSL (or KAFKA_ALLOW_INSECURE=1 on purpose, never with real data)")
    conf: dict = {"bootstrap.servers": servers, "security.protocol": protocol,
                  "client.id": f"governed-{service}"}
    if protocol in ("SSL", "SASL_SSL"):
        ca = _env(env, "KAFKA_CA_FILE")
        if ca:
            conf["ssl.ca.location"] = ca
        cert, key = _env(env, "KAFKA_CLIENT_CERT_FILE"), _env(env, "KAFKA_CLIENT_KEY_FILE")
        if bool(cert) != bool(key):
            raise KafkaConfigError("set both KAFKA_CLIENT_CERT_FILE and KAFKA_CLIENT_KEY_FILE, or neither")
        if cert:
            conf["ssl.certificate.location"], conf["ssl.key.location"] = cert, key
        conf["ssl.endpoint.identification.algorithm"] = "https"     # the broker's name must match its certificate
    if protocol.startswith("SASL_"):
        conf.update(_sasl(service, env, servers))
    conf.update(extra or {})
    return conf


def _sasl(service: str, env, servers: str) -> dict:
    mech = (_env(env, "KAFKA_SASL_MECHANISM") or "SCRAM-SHA-512").upper()
    if mech not in MECHANISMS:
        raise KafkaConfigError(f"KAFKA_SASL_MECHANISM must be one of {', '.join(sorted(MECHANISMS))}")
    if mech == "OAUTHBEARER":
        provider = (_env(env, "KAFKA_OAUTH_PROVIDER") or "").lower()
        return {"sasl.mechanisms": "OAUTHBEARER", "oauth_cb": oauth_callback(provider, servers, env)}
    tag = service.upper().replace("-", "_")
    user = _env(env, f"KAFKA_USERNAME_{tag}") or _env(env, "KAFKA_USERNAME")
    password = _secret(env, f"KAFKA_PASSWORD_{tag}") or _secret(env, "KAFKA_PASSWORD")
    if not user or not password:
        raise KafkaConfigError(f"SASL {mech} needs KAFKA_USERNAME_{tag} and KAFKA_PASSWORD_{tag} "
                               "(or KAFKA_USERNAME and KAFKA_PASSWORD)")
    return {"sasl.mechanisms": mech, "sasl.username": user, "sasl.password": password}


def oauth_callback(provider: str, servers: str, env):
    """An oauth_cb for confluent_kafka: returns (token, expiry in epoch seconds)."""
    if provider == "azure":
        host = _hosts(servers)[0]
        if not host.endswith(".servicebus.windows.net"):
            raise KafkaConfigError("KAFKA_OAUTH_PROVIDER=azure needs an Event Hubs namespace "
                                   "(<namespace>.servicebus.windows.net:9093)")
        scope = f"https://{host}/.default"

        def azure_token(_config: str):
            from azure.identity import DefaultAzureCredential  # optional dependency: the deploy group
            token = DefaultAzureCredential().get_token(scope)
            return token.token, float(token.expires_on)
        return azure_token
    if provider == "aws-msk-iam":
        region = _env(env, "AWS_REGION") or _env(env, "AWS_DEFAULT_REGION")
        if not region:
            raise KafkaConfigError("KAFKA_OAUTH_PROVIDER=aws-msk-iam needs AWS_REGION")

        def msk_token(_config: str):
            from aws_msk_iam_sasl_signer import MSKAuthTokenProvider  # optional dependency: the deploy group
            token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
            return token, expiry_ms / 1000
        return msk_token
    raise KafkaConfigError("KAFKA_SASL_MECHANISM=OAUTHBEARER needs KAFKA_OAUTH_PROVIDER=azure or aws-msk-iam")


def describe(conf: dict) -> str:
    """For startup logs: how this process connects, without any secret."""
    how = conf.get("security.protocol", "PLAINTEXT")
    if conf.get("sasl.mechanisms"):
        who = conf.get("sasl.username") or "token"
        how += f" {conf['sasl.mechanisms']} as {who}"
    if conf.get("ssl.certificate.location"):
        how += " with a client certificate"
    return f"Kafka {conf.get('bootstrap.servers')} ({how})"
