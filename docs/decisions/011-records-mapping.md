# 011: Records mapping instead of records code

Status: Accepted · Date: 2026-10-08

## Context
The records server was written for one clinic. Its code named the `patients` table, held
five hand-written queries (appointments, medications, visits, claims, patient details), the
sentence for each record, and which data class each record belonged to. Another organization,
or the same clinic with different table names, would have needed a programmer to rewrite it.
That contradicts the goal of a reference architecture other organizations can adopt.

## Decision
A profile describes its records database in `records.yaml`, and the records server follows it:

- **subject**: the table of people a question can be about (patients, applicants, members),
  its ID and name columns, and the "about this person" record.
- **records**: each record type's table, the column linking it to the subject, its data class,
  how its citable source ID is formed, an optional fixed filter (`only_where`), order, limit,
  title and text.
- **more_detail**: extra columns shown only to roles that may also see another data class
  (for the clinic: what a visit was for, `visit_clinical`, on top of `visit_dates`).

Rules:

- **No SQL in the mapping.** It names tables and columns only (letters, digits and `_`). The
  server builds every query itself, quotes every name and passes values as parameters. A
  profile file cannot run arbitrary SQL, change data or read a table it does not name.
- **Read only what the role may see.** A query selects only the columns the role's records
  use. A front-desk lookup reads a visit's date and type, never what it was for. The old code
  read those columns and left them out of the text; now they are never read.
- **Checked twice.** At load time: names, filters, limits, and that every data class is in
  `roles.yaml`. At server start: every table and column against the real database. A mismatch
  stops the server with the list of problems, never halfway through a request.
- `records_db` and `records_map` go together, and a mapping needs roles.

## Proof
Before changing the server, its output was recorded for every sample patient, every role,
every data class on its own, last-name lookups, an ambiguous name, a question with no name and
a role with no access: 70 cases, 107 records (`tests/data/records_golden.json`). The mapping-
driven server returns exactly the same output for all 70. A second test maps an unrelated
database (library members and loans) with no code change.

## Consequences
- Connecting another organization's data is a profile change: `records.yaml` plus its roles.
- SQLite only. The mapping format does not depend on SQLite, so PostgreSQL can be added
  without changing it.
- One kind of subject per profile, with records linked directly to it. Multi-step joins,
  free-text search across records, and matching on anything but a name are not supported yet.
- The tool that makes changes (rescheduling) is still written for the clinic; change tools
  stay code, reviewed one by one, behind human approval (ADR 009).