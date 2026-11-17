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
.venv/bin/python scripts/gateway_check.py              # as a clinician; a development caller is made and saved for you
```

For your own assistant, make a caller and use its token (`export GATEWAY_TOKEN=...` wins over the saved one):

```bash
.venv/bin/python -m ops.dev_identity add --role clinician --name "My assistant"
```

The check lists the tools this caller may use, asks for a sandbox test patient's medications, and
prints the record IDs and counts. `--show-text` prints the records.

Then see the rules work:

```bash
.venv/bin/python scripts/gateway_check.py --as front_desk        # medication_list is not one of its tools
.venv/bin/python scripts/gateway_check.py --as front_desk --tool search_records --query "Wilm Smart, born 1990-09-15"
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
| `profile.yaml` `sensitivity.text_model_mode` | `observe` (default): the audit records what the model would mark and withhold, and nothing changes. `enforce`: it marks records as the lists do |
| `outside.yaml` `rate_limit` | Calls one caller may make: `per_minute` and `burst` |
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

Built since the first trial, and tested only with stand-ins so far:

- **TLS on the gateway.** Set `GATEWAY_TLS_CERT` and `GATEWAY_TLS_KEY` and it serves https.
- **A production setting.** With `GOVERNED_ENV=production` the gateway refuses to start without TLS (its
  own, or `GATEWAY_TLS_AT_PROXY=1` to state that a proxy terminates it) or with a token check that is
  not an https address of your identity system. Demo sign-in and the development identity stand-in
  refuse to run.
- **A rate limit per caller** (`rate_limit` in outside.yaml; 120 a minute after a burst of 30 by default).
  A refused call is audited.
- **Nothing left to load on the first request.** The masking analyzer, the guard and any sensitive-category
  model load at start. If one cannot load, the gateway does not start.
- **The audit holds no dates or long numbers.** After masking, every date and every run of six or more
  digits in a logged question is replaced. `python -m eval.run_masking` measures what still gets through.
- **Observe mode for a sensitive-category model** (`text_model_mode: observe`, the default). The audit
  records what the model would have marked and withheld; nothing is withheld on its word.

Not built, or not done:

- **A container image for the gateway alone.** The repository's image is for the full pipeline.
- **Signed tokens (JWT).** Callers are identified by a token check only. Microsoft Entra ID does not
  offer one, so it needs an adapter or JWT validation in the gateway.
- **More than one gateway process.** The file audit trail is one writer per folder.
- **Your own value sets** for the sensitive categories. The lists in `privacy/sensitivity.yaml` are a
  development set and recognise named and coded information, not wording that only implies it.
- **A security review and a penetration test**, and the record vendor's and your own approval of the
  app.
