# Epic sandbox connection (ADR 013)

The `clinic-epic` profile is the clinic staff assistant with its records in Epic, read through
Epic's public FHIR R4 API. For development and demonstration it uses Epic's sandbox
(fhir.epic.com) and Epic's test patients. Not a real hospital, not real patients.

This project integrates with Epic through its public FHIR APIs, tested in Epic's sandbox. It is not
an Epic partner and is not certified or endorsed by Epic.

## The app at fhir.epic.com

- Audience: **Backend Systems** (system to system; SMART Backend Services).
- Incoming APIs, read-only, FHIR R4: Patient (read, search), MedicationRequest (read, search),
  Medication (read, search), Encounter (read, search). Nothing that creates or changes records.
- Non-Production JWK Set URL: this folder's `jwks.json`, served from GitHub:
  `https://raw.githubusercontent.com/vgalp/governed-agentic-ai/main/docs/epic/jwks.json`.
  It holds the **public** key only (key ID `gaai-sandbox-1`).

## Keys

The private key never enters this repository. Make the pair once, outside the repo:

```bash
mkdir -p ~/.config/governed-agentic-ai/epic && cd ~/.config/governed-agentic-ai/epic
openssl genrsa -out privatekey.pem 2048
chmod 600 privatekey.pem
```

`jwks.json` is the public half in JSON Web Key form (RS384, key ID `gaai-sandbox-1`). To replace
the key, make a new pair, publish the new `jwks.json` (a new key ID), then retire the old one.

## .env

```
EPIC_CLIENT_ID=<the app's Non-Production Client ID>
EPIC_PRIVATE_KEY_PATH=~/.config/governed-agentic-ai/epic/privatekey.pem
```

## Check the connection

```bash
.venv/bin/python scripts/epic_check.py                          # sign-in and server
.venv/bin/python scripts/epic_check.py --patient "Camila Lopez 1987-09-12"   # and one test patient
```

A new app or key can take a while to become active in the sandbox after it is saved.

## Finding a patient: name and date of birth

Epic's sandbox finds a patient by full name **and** date of birth, not by name alone. The profile
asks for both (`name_search` in fhir.yaml), which is also the usual patient-safety practice of two
identifiers. A question with only a name gets a reply asking for the date of birth, and nothing is
requested from Epic.

## Run the assistant on Epic's records

```bash
.venv/bin/python scripts/fetch_drug_labels.py --profile clinic-epic --patient "Camila Lopez 1987-09-12"
PROFILE=clinic-epic ./scripts/dev.sh restart
```

On the Governance view of where data goes: in the sandbox, patient records come from Epic's servers
over the internet. In a hospital they would come from the hospital's own Epic system.
