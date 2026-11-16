# The governed gateway

A way to let an AI assistant your organization already uses read patient records from Epic, Oracle
Health or MEDITECH, under rules you can show an auditor. The assistant connects to the gateway over
the Model Context Protocol (MCP). It never gets the address or the credentials of the record system.

What the gateway does on every call:

| | |
|---|---|
| Who is calling | Asks your identity system who the token belongs to, and which role. Never trusts the caller's word. |
| What it may use | Only the record tools that role may use. Read-only: nothing here changes a record. |
| What it may see | Only the kinds of record data the role may see, and only the sensitive categories (substance use, mental health, HIV and others) the role is given. |
| What may leave | A caller at an outside AI service gets no record data. Only callers inside your network do. |
| What is checked | A crisis or a clinical decision in a request is stopped, and the caller gets a fixed reply. |
| What is recorded | Every call and every decision, in a hash-chained audit trail that shows any change or gap. |

It runs without Kafka, without an AI model and without the chat page: OPA (the policy engine), three
record tool servers and the gateway itself.

**Status: a development build.** It has been tested with invented data and with the vendors'
sandboxes. It has not run in a hospital. Read "Before real use" below.

## Try it on one machine

You need Docker (for OPA), `uv`, and the guard classifier (`.venv/bin/python -m classifier.fetch
guard-modernbert-v7`). The default profile uses Oracle Health's open sandbox, which needs no
credentials. Stop the full stack first if it is running (`./scripts/dev.sh stop`).

```bash
./scripts/gateway.sh start
```

This starts OPA, the record tools, a **development-only** stand-in for an identity system (port 8120), and the
gateway at `http://127.0.0.1:8110/mcp`. It makes two local secrets in `.env` if they are missing
(`OPA_TOKEN`, `GATEWAY_INTROSPECT_SECRET`).

Add a caller and call the gateway as an assistant would:

```bash
.venv/bin/python -m ops.dev_identity add --role clinician --name "My assistant"
export GATEWAY_TOKEN=<the token it printed>
.venv/bin/python scripts/gateway_check.py
```

The check lists the tools this caller may use, asks for a sandbox test patient's medications, and
prints the record IDs and counts. `--show-text` prints the records.

Then see the rules work:

```bash
.venv/bin/python -m ops.dev_identity add --role front_desk --name "Front desk app"
export GATEWAY_TOKEN=<its token>
.venv/bin/python scripts/gateway_check.py                        # medication_list is not one of its tools
.venv/bin/python scripts/gateway_check.py --tool search_records --query "Wilm Smart, born 1990-09-15"
```

The front desk may look up a record and gets patient details and visit dates only. Records in a
sensitive category are withheld, and the result says how many, not which category.

Check the audit trail, then stop:

```bash
./scripts/gateway.sh verify
./scripts/gateway.sh stop
```

## Connect an assistant

Any MCP client that supports streamable HTTP and a bearer token:

- URL: `http://127.0.0.1:8110/mcp`
- Header: `Authorization: Bearer <the caller's token>`

The assistant sees only the tools its role may use. A tool call is a question in plain words, for
example `{"query": "What medications is Wilm Smart (born 1990-09-15) taking?"}`.

## Settings

Everything is in the profile folder (`profiles/clinic-oracle/`, `clinic-epic/`, `clinic-meditech/`).

| File | What it decides |
|---|---|
| `outside.yaml` | Where the gateway listens, and the address of your identity system's token check |
| `profile.yaml` `agents.outside` | Which tools outside assistants may be offered at all |
| `roles.yaml` | Per role: tools, kinds of record data, sensitive categories |
| `profile.yaml` `sensitivity` | Which sensitive categories are tracked, and your own lists if you have them |
| `profile.yaml` `sensitivity.text_model` | Optional: a trained classifier (a folder in `models/`) that also reads record text for wording the lists cannot know. Off unless named; see ADR 038 for what it has and has not been tested on |
| `fhir.yaml` | The record system: address, sign-in, limits, which resources become which records |
| `rules.yaml`, `responses.yaml` | What is stopped, and the fixed replies |

Environment:

| Variable | |
|---|---|
| `PROFILE` | The profile to serve (default `clinic-oracle`) |
| `AUDIT_SINK=file`, `AUDIT_DIR` | Write the audit trail to files in this folder (set by `gateway.sh`) |
| `AUDIT_HMAC_KEY` | Signs the audit trail, so someone who can edit the files cannot forge a valid chain. Not made for you: set it before first use in a real install |
| `GATEWAY_INTROSPECT_SECRET` | Sent with each token check, so only the gateway can ask |
| `OPA_TOKEN` | OPA answers only callers that hold it |

### Your identity system

The gateway sends `POST <auth.url>` with `{"token": "..."}` and the header `X-Introspect-Secret`. It
expects JSON: `{"active": true, "client_id": "...", "name": "...", "role": "nurse", "location":
"local"}`. `location` is `local` for an assistant running inside your network and `external` for one
at an outside AI service; anything else is treated as external. Any failure means no access.

Start with `GATEWAY_IDENTITY=own ./scripts/gateway.sh start` to leave the stand-in out.

## The audit trail

One file per kind of event in `AUDIT_DIR` (`audit.tool_calls.jsonl`, `audit.guardrails.jsonl`, ...),
one event per line. An event holds who called, which tool, the role, the decision, counts and
categories. Questions are stored masked. Record text is never stored.

`./scripts/gateway.sh verify` recomputes the chain and reports any changed, removed, reordered or
damaged event. On an ordinary disk this shows tampering; it does not prevent it. Put `AUDIT_DIR` on
storage the gateway can append to and nobody can rewrite.

## Before real use

Not built, or not done:

- **A container image for the gateway alone.** The repository's image is for the full pipeline.
- **TLS on the gateway.** It listens on plain HTTP on 127.0.0.1. Put it behind a reverse proxy that
  terminates TLS, or it must not be reachable from another machine.
- **Signed tokens (JWT).** Callers are identified by a token check only. Microsoft Entra ID does not
  offer one, so it needs an adapter or JWT validation in the gateway.
- **More than one gateway process.** The file audit trail is one writer per folder.
- **A rate limit per caller** on the gateway.
- **Your own value sets** for the sensitive categories. The lists in `privacy/sensitivity.yaml` are a
  development set and recognise named and coded information, not wording that only implies it.
- **A security review and a penetration test**, and the record vendor's and your own approval of the
  app.
