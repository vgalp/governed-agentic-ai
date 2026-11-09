# 031: Secured Kafka and private cloud deployment (Azure, AWS)

Status: Accepted · Date: 2026-11-08

## Context
Audit SC-3 was the last open blocker: unmasked text travels on Kafka, and Kafka had no TLS, no
sign-in and no access rules. Every process trusted the broker and the broker trusted every process.
It stayed open because the right fix depends on the hosting, and nothing described a hosting yet:
the repository ran on one machine only. Offices that pilot it run on Azure or AWS, and require that
no part of it is reachable from the internet.

## Decision
- **One way to connect** (`ops/kafka.py`). Every producer and consumer gets its settings from
  `kafka_config(service)`, built from `KAFKA_*` variables: PLAINTEXT, SSL or SASL_SSL; SCRAM or PLAIN
  with a username and password per service (or from a mounted secret file, `_FILE`); OAUTHBEARER
  with **Entra ID** (Event Hubs, any `DefaultAzureCredential`, such as a managed identity) or **MSK
  IAM** (the task's role); a CA file and client certificates. Plaintext to a broker on another
  machine is refused at startup unless `KAFKA_ALLOW_INSECURE=1` says so on purpose. Startup logs
  say how a process connects, never with a secret. Without settings nothing changes: the local
  broker, as before.
- **One access list** (`ops/kafka_access.yaml`): which service may write and read which topic, and
  which consumer groups it uses. The planner cannot write `chat.requests` or `approvals.decided`
  (it cannot forge a person's message or approve its own change); the API cannot write audit
  events; the audit verifier only reads. The same list becomes the local broker's ACLs, the MSK IAM
  policies and the Event Hubs role assignments, so the rules cannot drift apart between
  environments. `tests/test_kafka_access.py` holds it to the topics, subscriptions and group IDs the
  code actually uses.
- **A secured local broker** (`GOVERNED_KAFKA_SECURE=on ./scripts/dev.sh start`): a compose override
  with SASL_SSL and SCRAM-SHA-512 on both network listeners (a broker certificate from the local CA,
  `./scripts/certs.sh kafka`), Kafka's StandardAuthorizer denying anything not granted, no topic
  creation on first use, and administration only on listeners bound to the broker container's own
  loopback address. `scripts/kafka_secure.sh` makes a password per service (`.kafka.env`, mode 600,
  not committed), adds the users and ACLs, and `check` proves that a wrong password, plaintext and a
  forbidden write are refused. `dev.sh` gives each service only its own password. Kafka UI signs in
  read-only. Off by default until it has run on more machines, like mutual TLS (ADR 030).
- **Terraform for Azure and AWS** (`deploy/terraform/azure`, `deploy/terraform/aws`), private only:
  - *Azure*: a VNet with Event Hubs Premium (Kafka endpoint; public access and shared keys off;
    Entra ID only), Key Vault, a Premium container registry and storage, each behind a private
    endpoint with private DNS; an internal Container Apps environment; a GPU VM for Ollama without
    a public IP; outbound internet only through a NAT gateway, and only when `internet_egress`.
  - *AWS*: a VPC with private subnets and endpoints for ECR, logs, Secrets Manager, KMS, STS, SSM
    and S3; MSK with TLS and IAM sign-in only; ECS Fargate behind an internal load balancer; a GPU
    instance for Ollama administered through Session Manager (no SSH key, IMDSv2 only); one KMS key
    for everything stored; VPC flow logs.
  - On both: the API, the planner and the executor each run as their **own identity** with their
    own Kafka rights. The tool servers and OPA run beside the planner and the executor in the same
    task or app, so they share 127.0.0.1 as on one machine and the profiles do not change; OPA keeps
    its token and listens on 127.0.0.1 only. One replica each (the executor must not run twice).
    The audit key and OPA's token are generated and kept in the vault or Secrets Manager. Audit
    checkpoints and worker heartbeats go to shared files (Azure Files, EFS), so `/health` in the API
    sees the workers on other hosts.
  - The registry and the vault open to `operator_cidrs` only while deploying, if set; otherwise
    Terraform and the image push run from inside the network.
- **One image** (`Dockerfile`): every service from the same image, the cloud sign-in packages from
  the new `deploy` dependency group, OPA's binary, a non-root user, and only the repository's
  synthetic sample databases. `python -m ops.run_all` runs a profile's tool servers in one container.
- **Smaller changes**: `OLLAMA_HOST` (the name Ollama itself uses) points the guard and the local
  models at a GPU host; `KAFKA_IDEMPOTENCE=0` for a broker without idempotent producers.

## Consequences
- Tested here: the settings for every protocol and sign-in (mocked Entra ID and MSK tokens), the
  access list against the code, the compose override's listeners, the deployment's promises
  (no public address, Entra ID or IAM only), and `tofu validate` of both configurations (OpenTofu
  1.10, azurerm 4.40, aws 6.10). Not run here (no Docker or cloud account in this environment): the
  secured broker itself, and an apply; `./scripts/kafka_secure.sh check` and deploy/README.md are
  how to verify them.
- Event Hubs keeps events at most 90 days (Premium). The audit topics are also archived by Event
  Hubs Capture into a write-once container (unlocked at first; lock it once capture works). The
  executor rebuilds its state from the approval topics, so an approval older than 90 days is no
  longer known to it there; approvals expire long before that. On MSK the audit and approval topics
  are kept for good and the broker disks grow by themselves.
- Audit checkpoints on Azure Files or EFS can be changed by whoever can change the services;
  storage with a retention lock for them (ADR 030) is still open. So is the governed endpoint for
  outside AI clients (ADR 020) in the cloud configurations, a second planner for load, and GCP.
- uv.lock must be refreshed once (`uv lock`) for the `deploy` group before building the image.
