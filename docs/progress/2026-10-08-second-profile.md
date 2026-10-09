# A second use case from configuration only

Date: 2026-10-08 · Profile: `profiles/county-benefits/`

## Question
Can a second, unrelated organization use the platform by writing configuration, with no
code? The first use case is a primary-care clinic. The second is a fictional county
benefits office (Maple County Human Services): caseworkers look up applicants, case
status, missing documents, reported income and payments.

## Result: 0 lines of code
The profile is 11 files, 597 lines, all configuration and sample data:

| File | Lines | What it sets |
| --- | ---: | --- |
| `profile.yaml` | 86 | models, routing, privacy, classifier, agents, examples |
| `tools.yaml` | 18 | the same two read tools as the clinic, labelled `case_record` |
| `roles.yaml` | 36 | intake worker, eligibility worker, supervisor, quality auditor; 5 data classes |
| `records.yaml` | 57 | applicants, cases, documents, income, payments |
| `rules.yaml` | 63 | blocks eligibility decisions, judgments of honesty, safety risks |
| `responses.yaml` | 35 | fixed replies for caseworkers |
| `prompt.md` | 18 | system prompt |
| `classifier_policy.md` | 11 | layer 2 categories: safety risk, eligibility or integrity decision |
| `data/seed.sql` | 89 | 5 fictional applicants, 7 cases, 8 documents, 5 incomes, 4 payments |
| `data/knowledge_base.json` | 163 | 10 sample office policies |
| `data/README.md` | 21 | how to build the database |

Outside the profile: `.gitignore` now ignores `profiles/*/data/*.db` instead of the clinic's
database only. No Python file changed. The tests (`tests/test_county_profile.py`, 32 tests)
are test code, not part of the profile.

## What was checked
- The profile loads and its mapping matches the database (every table and column).
- Each role gets only its data classes: intake workers never get income or payments, and
  their lookups never read those tables; the auditor gets no case records.
- `phone` and `street_address` are in the database but not in `records.yaml`: they are never
  read (checked on the SQL the server runs).
- Every cited ID matches the profile's pattern; every office policy passes the answer rules.
- Guardrails block eligibility decisions ("Is Tom Brandt eligible?"), judgments of honesty
  ("Is Lena Kovacs lying?") and safety risks, in questions and in answers, while lookups and
  general policy questions ("Are college students eligible for SNAP?") pass.
- Live: OPA and the same two tool servers, started for this profile, called through the
  gateway: each role got exactly its records, the gateway dropped nothing, the auditor was
  denied the records tool, and the audit held masked text only.

Two rule bugs were found and fixed while building it, both in the profile's own rules: an
answer naming a person ("Dana Whitfield is not eligible") was not blocked, and general
policy wording ("when an applicant qualifies for expedited service") was.

## Gaps found
1. **Changes need code.** A supervisor releasing a held emergency payment needs a change
   tool, and change tools are written per profile (the clinic's reschedules appointments).
   The executor's result message and two lines in the web pages also assume appointments,
   and one policy reason says "the clinic is closed". This profile is read-only for now.
2. **The start script starts every server for every profile.** `scripts/dev.sh` also starts
   the appointments server, which can write, against this profile's database. The gateway
   and OPA refuse any call to it (it is not in this profile's `tools.yaml`), but it should not
   run at all. The script should start only the servers the profile lists.
3. **Not yet run live with the models.** The masking of these names and the classifier's
   behaviour with this profile's policy were not tested with Presidio and Llama Guard here.
   If the classifier flags plain lookups, a narrow exemption in `rules.yaml` (configuration)
   fixes it, as it did for the clinic.

## Update 2026-10-09: gap 1 closed
Changes are now configuration too ([ADR 012](../decisions/012-generic-change-tool.md)). One
change server makes every approved change, as each profile's `actions.yaml` describes it.
The county profile gained `release_payment`: an eligibility worker or a supervisor asks, a
different supervisor approves, and only a payment that is still `held` becomes `issued`.
Added to the profile: `actions.yaml` (31 lines), a tool entry, the executor agent, the
supervisor's grant, a request pattern, three replies and an example. Still no code in the
profile. The executor and the web pages now show the server's own summary, and the policy
reasons no longer mention a clinic.
