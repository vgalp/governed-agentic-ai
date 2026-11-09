# Deploying to Azure or AWS

Terraform (or OpenTofu) for a private deployment: nothing is reachable from the internet. People
reach the web app from inside the network, or from a network connected to it (VPN, ExpressRoute,
Direct Connect, peering). Why it is built this way: [ADR 031](../docs/decisions/031-secured-kafka-and-private-cloud-deployment.md).

| | Azure (`terraform/azure`) | AWS (`terraform/aws`) |
|---|---|---|
| Kafka | Event Hubs Premium, Kafka endpoint, private endpoint, Entra ID only (no keys) | MSK, TLS, IAM sign-in only, no public access |
| Who may use which topic | a role per event hub per service, from `ops/kafka_access.yaml` | an IAM policy per service, from `ops/kafka_access.yaml` |
| Services | internal Container Apps environment: api, planner, executor | ECS Fargate in private subnets: api, planner, executor; internal load balancer |
| Secrets | Key Vault (private endpoint, purge protection) | Secrets Manager with a KMS key |
| Images | Premium container registry, private endpoint | ECR (immutable tags, scanned, KMS) |
| Models | GPU VM with Ollama, no public IP | GPU instance with Ollama, no public IP, Session Manager |
| Audit archive | Event Hubs Capture into a write-once container | audit topics kept for good, disks grow by themselves |
| Shared files | Azure Files (audit checkpoints, worker health) | EFS (same) |
| Internet | none, or outbound only through NAT (`internet_egress`) | none, or outbound only through NAT (`internet_egress`) |

The tool servers and OPA run next to the planner and the executor (same app or task, sharing
127.0.0.1), so profiles work unchanged. Each service signs in as its own identity.

## Before you start

- Terraform 1.6+ or OpenTofu 1.6+, and the cloud's CLI signed in (`az login` / `aws configure`).
- A place to run from **inside the network** (a VM or build agent there, or a VPN), or your public
  address in `operator_cidrs` (Azure) during the deployment. The registry and the vault are not
  reachable from anywhere else.
- `uv lock` once in the repository, so the image gets the `deploy` packages.
- Remote state with encryption and locking (an Azure storage account or an S3 bucket). The state
  holds the generated audit key: treat it like the key itself.

## Steps

1. **Network, Kafka, registry, secrets, Ollama** (the apps are not created yet):
   ```bash
   cd deploy/terraform/azure            # or aws
   cp terraform.tfvars.example terraform.tfvars    # and edit it
   terraform init && terraform apply
   ```
   The GPU host installs Ollama and pulls the models by itself (needs `internet_egress = true`);
   that takes 10 to 20 minutes.
2. **Build and push the image** (from the repository root, from inside the network):
   ```bash
   docker build -t governed-agentic-ai:0.1.0 .
   # Azure
   az acr login --name <registry name>
   docker tag governed-agentic-ai:0.1.0 <registry>/governed-agentic-ai:0.1.0 && docker push <registry>/governed-agentic-ai:0.1.0
   # AWS
   aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
   docker tag governed-agentic-ai:0.1.0 <registry>:0.1.0 && docker push <registry>:0.1.0
   ```
   `terraform output registry` prints the address.
3. **The services**: set `deploy_apps = true` in `terraform.tfvars`, then `terraform apply` again.
   On AWS, create the topics once: run the command from `terraform output setup_command`. On Azure
   Terraform made them already (one event hub per topic).
4. **Check** from inside the network: open `terraform output api_url`, then `<api_url>/health`
   should answer `ok`: the broker, the API's readers, the planner and executor, and delivery.

Sign-in (ADR 028): set the OIDC settings in `app_env` (issuer, client ID, redirect URL on the
web app's private address) and register that address with the identity provider. On AWS, give the
load balancer a certificate (`certificate_arn`): sign-in needs HTTPS.

## After the first deployment

- **Azure: lock the audit archive.** Once capture files appear in the `audit-archive` container,
  lock its immutability policy (portal, or `az storage container immutability-policy lock`). A
  locked policy can be extended, never shortened or removed; that is the point.
- **Close the operator access** you opened with `operator_cidrs`: set it back to `[]` and apply.
- **Audit check by hand**: `python -m audit.verify` with your own identity (an Entra ID role "Azure
  Event Hubs Data Receiver" on the audit hubs, or an IAM role with read access to the audit topics)
  and the same `KAFKA_*` settings as the services (`terraform output kafka_bootstrap`).
- **A new image**: build and push a new tag (tags cannot be overwritten on AWS), change `image_tag`,
  apply. The executor stops before the new one starts.

## Not covered yet

GCP; the governed endpoint for outside AI clients (ADR 020) in the cloud; more than one planner;
retention-locked storage for the audit checkpoints. See ADR 031.

## Locally first

The same Kafka protections on one machine: `GOVERNED_KAFKA_SECURE=on ./scripts/dev.sh start`, then
`./scripts/kafka_secure.sh check` (see docs/quickstart.md).
