# Oracle Health sandbox connection (ADR 032)

The `clinic-oracle` profile is the clinic staff assistant with its records in Oracle Health
(Cerner Millennium), read through Oracle Health's public FHIR R4 API. For development and
demonstration it uses Oracle Health's sandbox and its test patients. Not a real hospital, not real
patients.

This project integrates with Oracle Health through its public FHIR APIs, tested in Oracle Health's
sandbox. It is not an Oracle partner and is not certified or endorsed by Oracle.

## Start here: the open sandbox (nothing to register)

The profile is set up for the **open sandbox**: read-only, test data only, no sign-in.

```
base_url: https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d
auth: none
```

```bash
.venv/bin/python scripts/fhir_check.py --profile clinic-oracle                               # server
.venv/bin/python scripts/fhir_check.py --profile clinic-oracle --patient-id 12724065         # one test patient by id
.venv/bin/python scripts/fhir_check.py --profile clinic-oracle --patient "Wilm Smart 1990-09-15"   # by name and birth date
```

The check prints which of the profile's resource types the server offers, then how many of each
record type it read for the patient. A record type the server refuses is marked ✗ and the others
still run. The check also prints the patient's names and birth date as the server stores them: the
sandbox patient 12724065 is stored as given name "WILM", family name "SMART", born 1990-09-15, so the
examples say "Wilm Smart". The open sandbox can be slow; the profile waits up to 45 seconds
(`server.timeout`) and tries a timed-out read once more.

Tested 2026-11-12 against the open sandbox: all six resource types offered; patient 12724065 read with
medications, visits, problems, allergies and vitals.

## Next: the secure sandbox or a system account

A hospital would not use the open sandbox. The app signs in as itself (SMART Backend Services),
with a **system account** registered in Oracle Health's developer console (code console):

1. Create a system account (an app with no user), for the R4 sandbox tenant.
2. Grant read scopes only: `system/Patient.read system/MedicationRequest.read system/Medication.read
   system/Encounter.read system/Condition.read system/AllergyIntolerance.read system/Observation.read`.
3. Sign-in: either register a public key (JWK Set) and keep the private key outside the repo, or
   use the account's secret. The client supports both.
4. In `profiles/clinic-oracle/fhir.yaml`, replace the `server:` block with the commented block below
   it (secure sandbox base URL, tenant token URL, scopes).

```
# .env, never committed
ORACLE_CLIENT_ID=<the system account ID>
ORACLE_PRIVATE_KEY_PATH=~/.config/governed-agentic-ai/oracle/privatekey.pem   # with a key ...
# ORACLE_CLIENT_SECRET=<the account secret>                                    # ... or a secret
```

Make the key once, outside the repo, as for Epic:

```bash
mkdir -p ~/.config/governed-agentic-ai/oracle && cd ~/.config/governed-agentic-ai/oracle
openssl genrsa -out privatekey.pem 2048 && chmod 600 privatekey.pem
```

Oracle Health access tokens last about ten minutes; the client asks for a new one when the old one is
about to end.

## How Oracle Health differs from Epic, and what the profile does about it

| | Epic sandbox | Oracle Health sandbox |
|---|---|---|
| Sign-in | signed key (Backend Services) | none (open), or key or secret (system account) |
| Resource IDs | letters, digits, dots | digits (e.g. `12724065`) |
| Source IDs | last 8 characters | last 8, or the whole ID when shorter (4 to 8) |
| Finding a patient | name and birth date | name and birth date; or by id in the check |


## Finding a patient by record number (MRN)

Each organization gives its record numbers its own system (an OID). Once you know it, add to
`subject:` in `profiles/clinic-oracle/fhir.yaml`:

```yaml
  identifier: {param: identifier, system: "urn:oid:<the organization's MRN system>"}
```

Then "Give me a brief on MRN 12345, born 1990-09-15" reads that patient directly; the birth date must
still match. Without it, patients are found by name and birth date.

## Run the assistant on Oracle Health's records

```bash
.venv/bin/python scripts/fetch_drug_labels.py --profile clinic-oracle --patient "Wilm Smart 1990-09-15"
PROFILE=clinic-oracle ./scripts/dev.sh restart
```

On the Governance view of where data goes: in the sandbox, patient records come from Oracle Health's
servers over the internet. In a hospital they would come from the hospital's own system.
