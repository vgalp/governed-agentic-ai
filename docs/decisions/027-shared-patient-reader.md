# 027: One reader for patients and their records, for both sources

Status: Accepted · Date: 2026-11-02

## Context
Three tools answer from a patient's record without a model: the medication review (ADR 017), the
medication list and the chart brief (ADR 023). Each carried its own copy of the same steps: open
the source (the database of `records.yaml` or the FHIR server of `fhir.yaml`), find the patient
named in the question, explain a miss ("more than one", "not found", "date of birth needed"), and
read a record type's rows. Three copies drift: the chart brief handled a FHIR resource type the app
is not granted yet, the review did not.

## Decision
`mcp_servers/common/patients.py` is the one copy: `Source.open(profile, kind)`, `find(query)`
returning a `Patient` or a `NotFound` with the reply to give, `records(type, patient, allowed)`
for the cited lines exactly as the records tools build them, and `rows(type, patient, fields)`
for structured values by column (database) or FHIR path. The medication review and the chart
brief use it; the FHIR per-resource resilience now applies to both. The replies are the words the
tools already used, so nothing changes for the person.

## Consequences
- The next record-reading tool (labs, "what changed since yesterday") is checks and wording,
  not a fourth copy of source handling.
- A records-source change (another FHIR server, another database) is made once.
- Not folded in yet: `medication_list`, which still calls the records servers' `search`
  directly, and the records servers themselves, which remain the owners of `find_subjects`,
  `_select` and `find_patient`; the shared reader calls them rather than copying them.
