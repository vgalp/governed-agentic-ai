# 032: Oracle Health and MEDITECH record systems

Status: Accepted · Date: 2026-11-12 · Oracle Health open sandbox: tested 2026-11-12 · MEDITECH Greenfield: pending

## Context
ADR 013 read records from Epic through FHIR R4, with the app signing in as itself (SMART Backend
Services). A physician and a hospital lead both asked about the other large U.S. record systems:
Oracle Health (Cerner Millennium) and MEDITECH Expanse, which many community and rural hospitals
use. Both offer FHIR R4, but they sign in differently:

- **Oracle Health** has an open sandbox (no sign-in, read-only, test data), and system accounts that
  sign in as the app, with a signed key or an account secret.
- **MEDITECH Greenfield** does not give outside apps backend access. A person signs in in the
  browser (authorization code with PKCE, a confidential client secret), and the token is for one
  patient. Refresh tokens keep the sign-in going.

## Decision
- **Three sign-in modes, chosen in `fhir.yaml`** (`server.auth`): `none`, `smart_backend` (a signed
  RS384 assertion, or `client_secret_env` for HTTP Basic) and `smart_user` (`fhir/smart_user.py`).
  The mapping still holds no secret: the loader refuses `client_id`, `client_secret`,
  `private_key`, `access_token` and `refresh_token` written as values, token and sign-in URLs must
  be https, and the token file must be a path outside the project.
- **Sign-in by a person happens once, outside the assistant** (`scripts/fhir_connect.py`): PKCE S256,
  a random state checked on return, the redirect served on localhost only, the code traded at once.
  Tokens go to an owner-only file (0600, folder 0700), written whole or not at all. The tool
  servers refresh from that file under a file lock, because several servers share it and the server
  may replace the refresh token on each refresh.
- **A sign-in for one patient is never searched around.** When the token names a patient, the
  records server reads that patient and checks the question names them with their date of birth
  (the same two identifiers a search would use). Nothing else is requested.
- **Two profiles, `clinic-oracle` and `clinic-meditech`,** copied from `clinic-epic`: same roles,
  data classes, guardrails, tools and records. Only `fhir.yaml` differs (server, sign-in, US Core
  `intent=order` for MEDITECH) and the source ID pattern accepts shorter numeric IDs (4 to 8).
- **One check script for every FHIR profile** (`scripts/fhir_check.py`): sign-in, capabilities, then
  one patient by name and birth date, by id, or the signed-in patient; a refused record type is
  reported and the others still run. `epic_check.py` remains as a shortcut.
- **A server that cannot be reached is a FhirError, not a crash,** so the assistant says the record
  system did not answer.
- The client stays GET-only toward FHIR servers. The only POSTs go to token endpoints.

## Consequences
- An office on any of the three systems adopts the assistant by choosing a profile and filling
  `.env`; no code changes.
- The Oracle open sandbox can be demonstrated with nothing registered. MEDITECH needs a Greenfield
  account and a browser sign-in; its token reaches one test patient only.
- Oracle Health's open sandbox was tested on 2026-11-12: the six resource types offered, and test
  patient 12724065 read with medications (20), visits (3), problems (20), allergies (20) and vitals
  (30). Visits and problems first timed out at 20 seconds; the profile now waits 45 seconds and a
  timed-out GET is tried once more.
- MEDITECH Greenfield is not yet tested live (it needs a signed access agreement and an app). Until
  then it is tested against a fake US Core server only (`tests/test_fhir_connectors.py`).
- Not certified or endorsed by Oracle or MEDITECH.
