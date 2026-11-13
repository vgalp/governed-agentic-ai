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

## Update (2026-11-12): hardening Epic and Oracle Health

A strict review before any clinical use found these, now fixed:

- **"Latest" and "recent" never trust the server's order.** No search asked for an order, so the
  brief picked the newest of 30 vitals and 3 visits in whatever order they came. A record type can
  now read a recent window (`since_days`: the `date=ge…` search parameter; vitals 2 years, visits
  3 years), sort what it read by its own date (`newest_first`) and keep the newest (`show`: the 3
  latest visits). If the window itself is cut off, the brief says the latest may be missing, under
  the heading and as a check.
- **A busy server is waited for:** 429, 502, 503 and 504 are retried twice, after the server's
  Retry-After (up to 10 seconds) or 1 then 2 seconds.
- **A refused token is replaced once:** a 401 drops the cached token (backend sign-in) or forces a
  refresh (sign-in by a person) and the request is tried again.
- **Signing in is thread-safe:** the parallel chart-brief reads share one token; only one sign-in
  runs at a time.
- **A patient can be found by record number** (`subject.identifier: {param, system}` in fhir.yaml,
  per hospital: the MRN's system differs between organizations): "MRN 7781, born 1971-03-04". The
  birth date must still match. Without it, lookup is by name and birth date as before.
- Home medications: Epic and Oracle Health record what a patient reports taking as
  MedicationRequest in FHIR R4 (Oracle's R4 API offers no MedicationStatement), so the existing
  medication search already includes them; nothing was added.

Still open (production): launching from the EHR with the clinician's own session (SMART EHR launch,
user-level scopes), so the hospital's audit names the clinician and the patient comes from the
chart; keys in a vault; replayed recordings of real sandbox responses; an Inferno (US Core) run.
(EHR launch and vaults: ADR 035. Circuit breaker, limits, metrics, validation, budgets, the
per-session cache, the capability check, replayed recordings and a shared session store: ADR 036.
Inferno remains.)
