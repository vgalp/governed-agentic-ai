# 012: One change server, changes described in the profile

Status: Accepted · Date: 2026-10-09

## Context
Approved changes (ADR 009) were made by a server written for the clinic: it moved
appointments, and only appointments. The executor's result message and the two web pages
assumed an appointment, and one policy reason said "the clinic is closed". The county
benefits profile (built from configuration only, see the 2026-10-08 progress note) needed a
supervisor to release a held payment, and that would have meant a second server, written
and reviewed for one office. Every new organization would need code for every kind of change.

## Decision
One change server (`mcp_servers/changes`) for every profile. Each action in `actions.yaml`
says what it changes in a `change:` block, using the record types already described in
`records.yaml` (ADR 011):

```yaml
change:
  record: payments            # record type: table, ID column, cited ID prefix
  id_field: payment_id        # the proposal field holding the cited ID (a source field)
  set: {status: issued}       # column <- fixed value
  set_from: {}                # column <- approved field value (the clinic: starts_at <- new_start)
  only_if: {status: held}     # the record must still look like this
done_text: "{id} released: status {before_status} -> {after_status}"
```

Rules:

- **The caller sends values, not instructions.** The executor sends the approved field
  values and the approval ID. Table, row, columns and precondition come from the profile on
  the server. Extra fields are ignored; a caller can't add a column or a value.
- **No SQL in the profile.** Names only, quoted by the server; values are parameters.
- **Some columns can never be written:** the record's ID column and the column linking it to
  its subject (a payment can't be moved to another applicant). A record type with shortened
  cited IDs (`keep_last`) can't be changed, because its cited ID may match more than one row.
- **A change is one row, once.** The server finds the row by the cited ID, checks `only_if`,
  then updates with the same conditions in the `WHERE` clause; anything but exactly one row
  changed rolls back. The update and its action-log entry (keyed by approval ID, before and
  after as JSON) are one transaction, so a repeated approval returns `already_done`.
- **One tool per action**, named in `tools.yaml`. The server registers each action's tool
  under its own name, so roles, OPA (`mcp_authz.rego`), the audit and the trace still see
  `release_payment` or `reschedule_appointment`, not a generic "change".
- **Checked twice,** like records: at load time (record type, field types, columns, done_text
  placeholders and filters) and at server start (every table and column against the database).
- Policy reasons are worded for any profile ("the proposed time is outside the allowed hours").

## Proof
The clinic's tests for the old server were moved to the new one unchanged in meaning: the
change is made, made once per approval, logged with its approval, refused for an unknown
ID, a bad time or no approval, and shows up in the records afterwards. The new server also
refuses to move a cancelled appointment, which the old one did too. The county profile
releases a held payment with no code: `tests/test_changes.py` (28 tests), and a live run of
the server over MCP for both profiles.

## Consequences
- A new kind of change is a profile change: an action, a tool entry and grants.
- Only single-row updates of existing records. Creating or deleting records, changing several
  rows, or changes that call another system (sending a letter, a payment run) still need code,
  reviewed one by one, behind the same approval step.
- Preconditions are equality checks only (`status = held`). Ranges or cross-record conditions
  belong in OPA (`actions.rego`), which already holds the time rules.