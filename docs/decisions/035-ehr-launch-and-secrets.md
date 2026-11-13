# 035: Launching from inside the EHR, and secrets from a vault

Status: Accepted · Date: 2026-11-13 · Live test against a vendor's launch: pending

## Context
The production review of the Epic and Oracle Health connectors (ADR 032) found the largest gap:
with backend sign-in the app reads as itself, so the hospital's own audit cannot say which clinician
read a chart, the app can read every patient, and the patient is found by name. Hospitals expect a
SMART launch from inside the EHR: the clinician opens the app from a patient's chart, and it reads
with that clinician's own access, on that chart. Separately, keys and secrets lived in files.

## Decision
- **SMART App Launch (EHR launch), `fhir/smart_launch.py`.** `/smart/launch` accepts a launch only
  from the EHR addresses the profile lists (`launch.issuers`; default its own server), discovers the
  EHR's endpoints, and sends the browser to its sign-in with the launch code, PKCE (S256), a state
  and a nonce. `/smart/callback` trades the code (confidential client: secret or signed assertion;
  public client: PKCE only), and requires a patient and an ID token signed by the EHR's published
  keys, for this app, unexpired, with this sign-in's nonce. The role comes from who the EHR says
  signed in (`fhirUser`'s type, mapped in `launch.roles`); a user type with no role is refused.
  States are single-use and expire after 10 minutes.
- **The profile's launch scope must be the user's own read scopes**: `launch openid fhirUser` and
  `user/<Type>.read`; the loader refuses write, `system/` and `patient/` scopes.
- **Sessions are server-side and encrypted** (`fhir/sessions.py`): the clinician's tokens, the
  patient, who and which role, in a file encrypted with a session key, named by a hash, readable by
  the owner only. The browser holds a random session ID in a cookie (HttpOnly; Secure and
  SameSite=None over https, for EHRs that frame the app). The token is refreshed when the EHR gave a
  refresh token; otherwise the session ends with the token.
- **The pipeline never carries the session ID.** `/chat` takes who is asking and the role from the
  session, whatever the request says, and puts the session's SHA-256 on the request. The gateway
  passes it only to record tools, as a hidden parameter no caller can set (a caller's own value is
  dropped), and the audit notes `launched` without the reference. The record tools load the
  session by that reference and read with the clinician's token, at the session's EHR address,
  re-checked against the profile's issuers.
- **The chart's patient, no name search.** Launched, the records, chart brief and medication tools
  read the patient the chart is open on. A question naming another birth date is refused: "open
  their chart and launch from there". Nothing is ever searched by name.
- **Secrets by reference** (`fhir/secrets.py`): `env:`, `file:` (owner-only, refused otherwise),
  `azure-keyvault:https://<vault>.vault.azure.net/secrets/<name>` and `aws-secretsmanager:<arn or
  name>`, signing in with the machine's own identity, cached five minutes so rotation is picked up.
  Backend sign-in, sign-in by a person and the launch take `client_secret_ref` / `private_key_ref`;
  the session key is `GAAI_SESSION_KEY_REF` (a key file is made once on one machine without it).

## Consequences
- With a launch, the EHR's audit names the clinician for each read, the EHR enforces what they may
  see, and the assistant cannot drift to another patient.
- Backend sign-in still works for profiles and tools that need it (and the CLI's checks).
- A shared session store is a folder on one machine; several machines need a shared store with the
  same three calls.
- Tested against a fake EHR that signs its own ID tokens (tests/test_smart_launch.py): every refusal
  above is a test. Not yet run against Epic's or Oracle Health's own launch; docs/smart-launch/ says
  how.
