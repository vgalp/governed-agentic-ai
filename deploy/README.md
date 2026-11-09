# Deploying to Azure or AWS

Private deployments: nothing is reachable from the internet. People reach the web app from inside
the network or from a network connected to it (VPN, ExpressRoute, Direct Connect, peering). Why it
is built this way: [ADR 031](../docs/decisions/031-secured-kafka-and-private-cloud-deployment.md).

```
deploy/
  modules/                 reusable Terraform modules, one per concern, no environment in them
    azure/  network  identities  kafka  secrets  registry  storage  ollama  apps  private-endpoint
    aws/    kms  network  kafka  secrets  registry  storage  ollama  apps
    shared/ ollama-cloud-init.sh.tftpl
  live/                    Terragrunt: what is deployed where
    azure/
      root.hcl             remote state, provider, names, tags, the Kafka access list
      _common/<unit>.hcl   each unit once: its module, its dependencies, its inputs
      dev/env.hcl          everything that differs between environments
      dev/<unit>/terragrunt.hcl   two includes, nothing else
      prod/...
    aws/  (the same)
```

Every environment has the same units; what differs (subscription or account, region, network range,
sizes, image tag) is in its `env.hcl`. A new environment such as `staging` is a copy of `dev/` with
a new `env.hcl`. A setting only one unit in one environment needs can go in an `inputs = { }` block
in that unit's `terragrunt.hcl`; it overrides the common one.

| | Azure | AWS |
|---|---|---|
| Kafka | Event Hubs Premium, Kafka endpoint, private endpoint, Entra ID only (no keys) | MSK, TLS, IAM sign-in only, no public access |
| Who may use which topic | a role per event hub per service, from `ops/kafka_access.yaml` | an IAM policy per service, from `ops/kafka_access.yaml` |
| Services | internal Container Apps environment: api, planner, executor | ECS Fargate in private subnets, internal load balancer |
| Identity per service | a managed identity each | a task role each |
| Secrets | Key Vault (private endpoint, purge protection) | Secrets Manager with the environment's KMS key |
| Images | Premium registry, private endpoint | ECR, immutable tags, scanned |
| Models | GPU VM with Ollama, no public IP | GPU instance with Ollama, no public IP, Session Manager |
| Audit archive | Event Hubs Capture into a write-once container | audit topics kept for good, disks grow by themselves |
| Shared files | Azure Files (audit checkpoints, worker health) | EFS (same) |
| Internet | none, or outbound only through NAT (`internet_egress`) | the same |
| Guard rails | a subscription per environment in `env.hcl` | `allowed_account_ids`: dev settings cannot touch the prod account |

Dependency order (Terragrunt works it out): Azure network → identities → kafka, secrets, registry,
storage, ollama → apps. AWS kms → network → kafka, storage, ollama (and secrets, registry) → apps.

## Before you start

- [Terragrunt](https://terragrunt.gruntwork.io) 0.93+ with OpenTofu 1.10+ (or Terraform 1.10+), and
  the cloud's CLI signed in (`az login` / `aws sso login`).
- A place to run from **inside the network** (a VM or build agent there, or a VPN), or, on Azure,
  your public address in `operator_cidrs` while deploying. The registry and the vault are not
  reachable from anywhere else.
- `uv lock` once in the repository, so the image gets the `deploy` packages.
- Fill in `live/<cloud>/<environment>/env.hcl`: subscription or account, region, `unique` (Azure),
  the SSH key (Azure), network range, sizes.

### State, once per cloud

The state holds the generated audit key: keep it encrypted, versioned and limited to the people who
deploy.

- **Azure**: a resource group and a storage account (`state` in `env.hcl`) with a `tfstate` container,
  shared keys off, versioning on; give yourself "Storage Blob Data Contributor" on it:
  ```bash
  az group create -n governed-tfstate-rg -l eastus2
  az storage account create -n governedtfstate -g governed-tfstate-rg --sku Standard_ZRS \
    --min-tls-version TLS1_2 --allow-shared-key-access false --allow-blob-public-access false
  az storage account blob-service-properties update -n governedtfstate -g governed-tfstate-rg --enable-versioning true
  az storage container create -n tfstate --account-name governedtfstate --auth-mode login
  ```
- **AWS**: the bucket in `env.hcl`; Terragrunt makes it (versioned, encrypted, public access blocked)
  on the first run with `--backend-bootstrap`. Locking uses S3 itself (no DynamoDB table).

To try the configuration without any of this: `GOVERNED_TG_LOCAL_STATE=1 terragrunt run --all validate`
(state on this machine; never for a real environment).

## Steps

Run from `deploy/live/<cloud>/<environment>`, for example `deploy/live/azure/dev`.

1. **Everything except the services** (the image does not exist yet):
   ```bash
   terragrunt run --all plan  --queue-exclude-dir apps
   terragrunt run --all apply --queue-exclude-dir apps      # AWS, first time: add --backend-bootstrap
   ```
   The GPU host installs Ollama and pulls the models by itself (needs `internet_egress = true`): 10 to
   20 minutes.
2. **Build and push the image** (repository root, from inside the network). The registry's address:
   `terragrunt output -raw login_server` in `registry/` (Azure) or `terragrunt output -raw url` (AWS).
   ```bash
   docker build -t governed-agentic-ai:0.1.0 .
   # Azure
   az acr login --name <registry name>
   docker tag governed-agentic-ai:0.1.0 <login_server>/governed-agentic-ai:0.1.0
   docker push <login_server>/governed-agentic-ai:0.1.0
   # AWS
   aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
   docker tag governed-agentic-ai:0.1.0 <url>:0.1.0 && docker push <url>:0.1.0
   ```
3. **The services**: `cd apps && terragrunt apply`. On AWS, create the Kafka topics once with the
   command from `terragrunt output -raw setup_command`; on Azure the kafka unit made them already.
4. **Check** from inside the network: `terragrunt output -raw api_url` in `apps/`, then
   `<api_url>/health` should answer `ok`: the broker, the API's readers, the planner and executor,
   and delivery.

Sign-in (ADR 028): put the OIDC settings in `app_env` in `env.hcl` (issuer, client ID, redirect URL on
the web app's private address) and register that address with the identity provider. On AWS, give
the load balancer a certificate (`certificate_arn`): sign-in needs HTTPS.

## Changes and releases

- **A new version**: build and push a new tag (tags cannot be overwritten on AWS), set `image_tag`
  in dev's `env.hcl`, `cd apps && terragrunt apply`; when dev is good, the same tag in prod's. The
  executor stops before the new one starts.
- **A change to a module** reaches every environment that applies it; plan dev first
  (`terragrunt run --all plan`), then prod.
- **Who may use which topic**: change `ops/kafka_access.yaml` (its tests must pass), then apply the
  kafka unit (Azure) or the kafka and apps units (AWS).

## After the first deployment

- **Azure: lock the audit archive.** Once capture files appear in the `audit-archive` container,
  lock its immutability policy (portal, or `az storage container immutability-policy lock`). A
  locked policy can be extended, never shortened or removed; that is the point.
- **Close the operator access** you opened with `operator_cidrs`: set it back to `[]` and apply the
  secrets and registry units.
- **Audit check by hand**: `python -m audit.verify` with your own identity (on Azure the role "Azure
  Event Hubs Data Receiver" on the audit hubs; on AWS a role that may read the audit topics) and the
  same `KAFKA_*` settings as the services.

## Not covered yet

GCP; the governed endpoint for outside AI clients (ADR 020) in the cloud; more than one planner;
retention-locked storage for the audit checkpoints. See ADR 031.

## Locally first

The same Kafka protections on one machine: `GOVERNED_KAFKA_SECURE=on ./scripts/dev.sh start`, then
`./scripts/kafka_secure.sh check` (docs/quickstart.md).
