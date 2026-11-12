# MEDITECH Greenfield connection (ADR 032)

The `clinic-meditech` profile is the clinic staff assistant with its records in MEDITECH Expanse,
read through MEDITECH's FHIR R4 API (US Core). For development and demonstration it uses
**MEDITECH Greenfield**, MEDITECH's developer sandbox, and its test patients. Not a real hospital,
not real patients.

This project integrates with MEDITECH through its public FHIR APIs, tested in MEDITECH's Greenfield
sandbox. It is not a MEDITECH partner and is not certified or endorsed by MEDITECH.

## How Greenfield signs in (and why it differs from Epic)

Greenfield does not give outside apps backend (system) access. A **person** signs in, in the
browser (SMART authorization code with PKCE), and the token is for **one test patient**. So:

- You sign in once with `scripts/fhir_connect.py`. The tokens are kept in
  `~/.config/governed-agentic-ai/meditech/token.json`, readable by you only, never in the repo.
- The tool servers refresh the access token themselves (about an hour each) with the refresh token,
  until it ends or is revoked; then sign in again.
- The assistant can read only that patient. A question must still name the patient with their date
  of birth (two identifiers); the patient is read and checked against both. Nothing is searched.

In a hospital the same profile would use the hospital's MEDITECH endpoints and whatever access the
hospital grants; the sign-in mode is one line in `fhir.yaml`.

## Register the app in Greenfield

1. Create a Greenfield account and an app (confidential client).
2. Redirect URI: exactly `http://localhost:8765/callback`.
3. Scopes: `openid fhirUser launch/patient offline_access patient/*.read` (the wildcard is allowed
   in Greenfield only; a hospital would list each resource type).
4. Copy the client ID and secret into `.env`:

```
# .env, never committed
MEDITECH_CLIENT_ID=<the app's client ID>
MEDITECH_CLIENT_SECRET=<the app's client secret>
```

## Sign in and check

```bash
.venv/bin/python scripts/fhir_connect.py --profile clinic-meditech            # opens the browser
.venv/bin/python scripts/fhir_connect.py --profile clinic-meditech --status   # signed in? until when?
.venv/bin/python scripts/fhir_check.py --profile clinic-meditech              # server and the signed-in patient
.venv/bin/python scripts/fhir_connect.py --profile clinic-meditech --sign-out # delete the token file
```

Sign in with the account registered for Greenfield. The code the browser brings back is good for 60
seconds; the script trades it at once. If your browser does not open, add `--no-browser` and open
the printed link yourself.

`fhir_check.py` prints the test patient's name. Put that name and birth date in the `examples` of
`profiles/clinic-meditech/profile.yaml` and `ui/demo.html`, so the example buttons name a real test
patient.

## US Core details the profile handles

- Medication orders are searched with `intent=order` and the patient, as US Core servers require.
- If a combined search is refused (for example problems by category **and** clinical status),
  `fhir_check.py` marks that record type ✗ and the rest still run; drop the second parameter in
  `fhir.yaml`.

## Run the assistant on MEDITECH's records

```bash
PROFILE=clinic-meditech ./scripts/dev.sh restart
```
