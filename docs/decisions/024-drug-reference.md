# 024: Drug reference — the FDA label quoted, for drugs and never for patients

Status: Accepted · Date: 2026-11-02

## Context
A hospitalist's second request after the chart brief: look a medication up, not only the ones on a
patient's list — what it is for, contraindications, interactions, use in renal impairment — the
way a clinician opens the package insert or a reference product. The medication review (ADR 017)
already fetches FDA labels from openFDA through RxNorm for the patient's own medications; the
reference is the same labels asked about any drug.

The risk is the line the clinic profile draws: the assistant never makes a medication decision.
A reference tool must not become a way to ask "how much should I give" with different words.

## Decision
### 1. A tool that quotes, with no model (`mcp_servers/drug_reference`)
For the medication(s) named in the question, the tool returns the label sections the question asks
for (indications, boxed warning, contraindications, warnings, interactions, adverse reactions, use
in specific populations), verbatim, each label with its version date, DailyMed link and an `lbl-`
ID the grounding check knows. `answer: direct`: no model rewords the label. Two drugs in one
question get each label's mention of the other, with the review's own matching.

### 2. Drugs, never patients — by construction
The tool does not receive identifiers: it gets the masked question. A placeholder for a person in
the question means it was about a patient, and the tool refuses with a pointer to the medication
review and the chart brief, which read the record under role control. A patient's name therefore
cannot reach the label lookup, and the lookup cannot be used to reason about a patient.

### 3. No dosage, and a decision stays a decision
`drug_reference.yaml` lists the quotable sections; `dosage_and_administration` is not among them by
default, and a question that mentions dosing is told so. An office that wants nurses to read the
dosage section verbatim can add the line; the choice is recorded in configuration. Decision words
("should we", "can I give", "how much") cancel the classifier exemption that lets plain lookups
through, so such a question is handled by the rules and the classifier as before.

### 4. Public, dated, remembered
Labels come from openFDA (FDA), the drug name resolved through RxNorm (NLM): public U.S. services,
no key, no account; only the drug name is sent. The label is fetched on demand the first time, kept
in `data/drug_reference.json` with the date fetched, and quoted with the label's own version date.
A word that RxNorm does not match well is remembered as not a drug, so ordinary words are looked up
once. `fetch: never` restricts a profile to labels already on file.

## Consequences
- Clinicians and nurses can read the label for any drug inside the governed assistant, with the
  same audit trail as a records question; the front desk and the public profile cannot.
- The two-drug check reuses the review's matching and class words, so "warfarin and ibuprofen"
  answers from both labels without a patient.
- Not UpToDate: no synthesis, no guideline reasoning, no "in practice". Reference text under role
  control, with the version date, which is what a compliance office asks of an AI tool and what
  a reference product does not provide. Labels can be long, dated or missing (a drug with no label
  found says so).
- A pre-existing gap noted while testing: the input rule for medication decisions keys on words
  like "dose" or "medication", so "how much apixaban should we give" relies on the classifier,
  which flags it and gets no exemption. Worth a rule term for bare drug names in the next revision.
