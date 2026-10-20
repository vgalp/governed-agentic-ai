# 013. FHIR records source (Epic-ready)

Status: Accepted · Proposed 2026-10-11 · Implemented 2026-10-19 · Epic sandbox test: pending

## Context

Records come from a SQLite database through `records.yaml` (ADR 011): the mapping names tables and
columns, the records server builds every query itself, read-only, and a role's records are built only
from the data classes it may see.

A physician who saw the clinic demo (2026-10-11) said the real value is integration with Epic, which
most U.S. hospitals use. Epic and the other large U.S. record systems (Oracle Health, Meditech) expose
patient data through HL7 FHIR R4 APIs and support SMART on FHIR for app launch and sign-in. Epic
offers a public developer portal with a sandbox (fhir.epic.com).

## Decision

1. **A second kind of records source: FHIR.** A profile names `fhir: fhir.yaml` instead of
   `records_db` and `records_map` (one records source per profile). The ideas stay
   the same as ADR 011: a subject (the Patient), record types, data classes, templates, citable source
   IDs. What changes is where values come from: a record type names a FHIR resource type and search
   parameters instead of a table, and its texts use simple FHIR paths (`period.start`,
   `medicationCodeableConcept.text`, `reasonCode[0].text`) instead of column names. No code per
   organization; another FHIR server is another mapping.
2. **The same safety properties as the database source.**
   - Read-only: the client only performs FHIR `search` and `read` (HTTP GET). It has no code path that
     creates, updates or deletes.
   - Need-to-know: a resource type is requested only when the caller's role may see its data class,
     so data a role may not see is never fetched. Where the server supports it, `_elements` limits
     each response to the fields the mapping uses.
   - Checked at startup: the mapping is compared with the server's CapabilityStatement (resource types
     and search parameters). A mismatch stops the server, as a missing column does today.
   - The records server stays the only component that receives real names. The gateway audits the
     masked question, as now.
3. **Authentication by environment.** Local test server: none. Epic sandbox: SMART Backend Services
   (client credentials with a signed JWT; the private key stays in `.env`, never in the repo).
   Signing in as the clinician through a SMART on FHIR launch, so that the role comes from the
   record system instead of the demo's role picker, is a separate decision (ADR 014).
4. **Changes stay proposals.** This decision covers reading only. Writing back (for example moving an
   Appointment) keeps the approval flow of ADR 009 and ADR 012 and is decided separately.
5. **Test data: synthetic only.** Synthea's FHIR R4 export, loaded into a local HAPI FHIR server
   (Docker), with the same seeds as the SQLite data. Epic's sandbox uses Epic's own test patients.

## Example

    source: fhir
    subject:
      noun: patient
      resource: Patient
      search_by_name: name
      record:
        data_class: patient_details
        title: "Patient: {name}"
        text: "{name}, born {birthDate}, sex {gender}, lives in {address[0].city}."
    records:
      medications:
        resource: MedicationRequest
        link: patient
        only_where: {status: active}
        data_class: medications
        title: Current medication
        text: "{subject}: {medicationCodeableConcept.text}, on the list since {authoredOn|date}."

## Consequences

- One mapping format for a local database and for FHIR servers, so a hospital on Epic, Oracle Health
  or Meditech connects by writing a mapping, not code.
- Claims are not part of the FHIR mapping at first: billing data is usually not available to this
  kind of app. The front-desk role then sees no claims from a FHIR source.
- Name matching uses the server's `name` search, which behaves differently from the database's exact
  match. Ambiguous matches still return nothing ("use the full name").
- Wording: this project "integrates with Epic through its public FHIR APIs, tested in Epic's
  sandbox". It is not an Epic partner, certified or endorsed.
## Implementation (2026-10-19)

- `fhir/client.py`: GET-only FHIR R4 client (read, search with paging that never follows a link off
  the server, capability). SMART Backend Services sign-in: a JWT signed with RS384, key ID in the
  header, exchanged for a short-lived access token, cached until shortly before it expires. The
  client ID and the private key's path come from `.env`; the loader refuses them written in the file.
- `profiles/fhir_map.py`: the mapping (`fhir.yaml`): subject, record types, data classes, templates
  with FHIR paths, alternatives (`{a,b}`) and date filters, source IDs from resource IDs.
- `mcp_servers/fhir_records` (port 8104): the same `search_records` tool and record shape as the
  database source. A resource type is requested only for the data classes the role may see. The
  patient is found by given and family name from the question; two matches or none return nothing.
- `profiles/clinic-epic`: the clinic assistant on Epic's sandbox. Its rules, replies, prompt and guard
  policy are the clinic's (a test checks they have not drifted); roles have no appointments or claims,
  because the Epic app reads Patient, MedicationRequest, Medication and Encounter only.
- The medication review (ADR 017) reads MedicationRequest, follows a medicationReference to its
  Medication for the RxNorm code, and finds ingredients by code.
- The Epic app: Backend Systems audience, those four resource types, read and search only. Its public
  key is published as a JWK Set (`docs/epic/jwks.json`); the private key stays outside the repository.
- Tests: `tests/test_fhir.py`, against a fake FHIR server with invented resources (no network).
- `scripts/epic_check.py` checks sign-in, the server's capabilities and one test patient. The record of
  the first successful sandbox run (date, what was read) is added here when it is done.

Not yet: the CapabilityStatement check at startup only reports (the server may not be reachable when
the pipeline starts); `_elements` is not used yet; appointments and writing back are separate decisions.
