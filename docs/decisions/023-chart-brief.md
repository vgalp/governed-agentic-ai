# 023: Chart brief — the record assembled and checked, with no model writing a fact

Status: Accepted · Date: 2026-11-02

## Context
After the first demonstration to a hospitalist (internal medicine, U.S.), the request was not for
a better chatbot but for the chart: before rounds, the patient's history put together — problems,
medications and why each is prescribed, allergies, vitals — and checked: "on a blood thinner, why?",
"hypertension on the list, on what?", and "how accurate is it?". Products that answer evidence
questions (what the literature says) exist; the gap is assembling and verifying *this patient's*
record, which is a records problem and the one place the governance layer matters most, because a
wrong summary leads to a wrong decision.

The medication review (ADR 017) set the pattern: a tool reads the record, writes the answer from
the record's own text with IDs, and no model rewords it (`answer: direct`).

## Decision
### 1. The brief is the record's own lines, grouped and cited
`mcp_servers/chart_brief` reads one patient's problem list, current medications with the recorded
reason, allergies, latest vitals and recent visits, from the profile's records source (the database
through `records.yaml`, or Epic through `fhir.yaml`, ADR 013), and returns them grouped under
headings, every line ending in the record's ID. The tool's text is the answer; a model never sees
it before the person does. The grounding check knows the new IDs (`cond-`, `alg-`, `vit-`).

### 2. The checks are documentation checks against lists the office writes
`chart_brief.yaml` holds three lists and three thresholds, and the tool compares record fields with
them:
- **medication with no recorded reason:** the reason field is empty *and* none of the usual
  reasons the office lists for that medication is on the problem list ("No reason recorded for
  omeprazole, and the problem list has none of: reflux, GERD, ulcer…"). A medication the office did
  not list is reported only if its reason field is empty.
- **problem with no recorded treatment:** the problem is on the list and none of the medications
  the office expects for it is on the current list.
- **vital sign outside the office's thresholds:** the latest value of each kind only.
- the medication review's count of FDA label mentions, pointing to the full review.

Every finding is worded as what the record does or does not show, never as what to do, and the
brief ends with "nothing here is a recommendation". The lists are the office's: an office that does
not want a check deletes the line. This keeps the tool on the right side of the line the profile
draws: it never decides, it shows.

### 3. Roles, as everywhere
Clinicians and nurses have the tool; the front desk does not (tools.yaml and roles.yaml), and a
section whose data class the role may not see is left out and named. On Epic, a resource type the
role may not see is never requested; a resource type the server refuses (not yet granted to the
app) is reported as unread and the rest of the brief stands — the records lookup got the same
per-resource resilience, so adding resource types to `fhir.yaml` cannot break what worked.

### 4. Accuracy is measured, not estimated
`eval/chart_brief_accuracy.py` generates synthetic charts with planted issues, builds every brief
through the real tool code against a real database, and checks every cited line against its row
(ID exists, belongs to the patient, line carries the record's text and value), then scores the
checks. First run: 300 charts, 4,030 cited lines, 0 faithfulness errors, precision and recall 1.000
on all three checks (`eval/results/chart_brief_accuracy.md`). That is the design working, not a
model behaving; the number an office cannot get from a model-written summary.

## Consequences
- A hospitalist's pre-rounding question is answered from the record, with every line checkable,
  on a laptop, in seconds, for both records sources; the same `chart_brief.yaml` serves both.
- Three more FHIR read APIs for the Epic app (Condition, AllergyIntolerance, Observation); until
  granted, those sections read "could not be read" and the rest works.
- The synthetic demo patients (`demo_patients.yaml`) now carry charts chosen to trip each check
  once; `build_db.py --add-chart` adds the tables to an existing database (run by `dev.sh start`).
- The checks are only as good as the office's lists; the accuracy run measures the mechanism, not
  the lists' clinical completeness. A clinical reviewer should read `chart_brief.yaml`.
- Not done: trend lines (vitals over time), labs, and the problem-medication map beyond the
  demo's entries. Each is a list in the same file or a section in the same tool.

## Update (2026-11-12): checks from the first Oracle Health brief

The first brief from Oracle Health's sandbox (ADR 032) showed what a real record system's data does
that the synthetic charts did not:

- **Repeated entries are shown once.** The same medication ordered 15 times is one line: the latest
  entry, with how many there are and the dates they span. The checks run once per medication too.
- **Two new checks:** the same medication in several current orders, and the same ingredient in
  several current medications (acetaminophen in Tylenol and in Tylenol PM), from the RxNorm
  ingredients saved with the label file. Facts of the record, not a judgment of the doses.
- **Vital signs:** a threshold matches the record's other words for it (`names`: "Temperature Oral",
  "Pulse Sitting"); a value in other units is not compared (no conversion); a value outside
  `plausible` is reported as one a measurement could not be (11.1 °C), and a date after today as a
  future date.
- **A problem that shares a word with another** is not checked as that one (`problem_not`: chronic
  venous hypertension is not hypertension), in both the medication-reason and the treatment checks.
- **Wording:** no doubled "Allergy to"; a missing date says "date not recorded" instead of "unknown".

The accuracy run (eval/chart_brief_accuracy.py, 200 charts, seed 7) is unchanged after this: 0
errors in 2,687 cited lines; precision and recall 1.000 for each check.

## Update (2026-11-12): a list read only in part says so

Wilm Smart's sandbox record returned exactly 20 allergies, a different 20 on each run: the limit,
not the record. A partial allergy or medication list shown as the whole one is the worst thing a
chart summary can do. Now:

- The FHIR client reads in pages of 50 up to the record type's limit (fhir.yaml; medications and
  allergies 200, problems 100, at most 250) and reports whether the server holds more (a `next`
  link left, or a `total` larger than what was read).
- For the sections that must be whole (`complete:` in chart_brief.yaml: problems, medications,
  allergies), a cut-off list says under its heading "N shown; the record has more. Check it
  directly", and the first check says the section is incomplete and the checks may miss entries.
  Vitals and visits are the latest only and are not warned about.
- The database source reads these three types without a limit, so it is never cut off.
