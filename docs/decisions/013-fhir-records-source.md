# 013. FHIR records source (Epic-ready)

Status: Proposed · Date: 2026-10-11

## Context

Records come from a SQLite database through `records.yaml` (ADR 011): the mapping names tables and
columns, the records server builds every query itself, read-only, and a role's records are built only
from the data classes it may see.

A physician who saw the clinic demo (2026-10-11) said the real value is integration with Epic, which
most U.S. hospitals use. Epic and the other large U.S. record systems (Oracle Health, Meditech) expose
patient data through HL7 FHIR R4 APIs and support SMART on FHIR for app launch and sign-in. Epic
offers a public developer portal with a sandbox (fhir.epic.com).

## Decision

1. **A second kind of records source: FHIR.** `records.yaml` may say `source: fhir`. The ideas stay
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