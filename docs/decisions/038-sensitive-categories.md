# 038: Sensitive categories of health information

Status: Accepted · Date: 2026-11-14 · Steps 1 and 2 of 3 built (detection, and enforcement in the four clinic profiles). The text model is not built

## Context
A hospital that connects an AI assistant to its records has two fears the pipeline does not yet
answer.

- **Some information is protected more strictly than the rest.** Substance use treatment (42 CFR
  Part 2), mental health, HIV, sexual and reproductive health, abuse and genetics each have their own
  rules, in federal or state law, about who may see them and when they may be disclosed.
- **Today the pipeline knows a record's kind, not its content.** Roles see data classes
  (`medications`, `problems`): a role that may see medications sees methadone beside lisinopril. The
  router keeps record data and anything with a name away from outside models, but a question with no
  name in it ("a patient just told me she is using heroin again, what do I chart?") can go out.

A layer that sorts information by category would let policy say "the front desk does not see
substance use treatment" and "nothing about HIV leaves the building", and would put the categories
of every answer in the audit trail.

This is not a new idea. HL7 defines security labels for these categories, and data segmentation
products exist. Their known weakness is recognising the category in the first place, most of all in
free text. That is the part a model can add, and the part that has to be measured.

## Decision
Build it in three steps, each measured before the next.

**Step 1 (this ADR, built): recognise the category.** `privacy/sensitivity.py` says which categories
a FHIR resource or a text falls in. It decides nothing.

- **Eight categories**, in `privacy/sensitivity.yaml`: substance use, mental health, HIV, sexually
  transmitted infections, reproductive health, abuse and violence, genetics, and "marked restricted
  by the source system".
- **Five kinds of evidence**, strongest first:
  - `label`: the EHR's own security label on the record (`meta.security`, HL7 v3 ActCode and
    Confidentiality codes). Trusted as it is.
  - `code`: an ICD-10-CM code in the category's list.
  - `medication`: a medication mostly used for the category.
  - `text`: a phrase in the category's list.
  - `model`: a text classifier, when one is given. None exists yet.
- **A finding carries the code or list entry that matched, never the record's words**, so it can be
  logged. The audit form is categories and kinds of evidence only.
- **A development set and a scorer** (`eval/datasets/sensitivity_dev_v1_protocol.md`,
  `eval/run_sensitivity.py`): 126 synthetic rows, 104 texts and 22 FHIR resources, split by whether
  the text names the category, only implies it, or only looks sensitive.

**Step 2 (built): act on it.** A profile turns the layer on with a `sensitivity:` section naming the
categories it tracks. The four clinic profiles do.

- **Records are marked.** The gateway marks each record a role-filtered tool returns with its
  categories, from the record's title and text. The record tools add what only the FHIR resource
  shows (its security labels and ICD-10 codes), as `sensitivity` on the record. A tool can add a
  category; it cannot remove one.
- **A role sees only its categories.** `sensitive_categories` on a role in roles.yaml lists them; OPA
  answers (`roles/allowed_sensitive`); the gateway withholds every record in another category. A
  role lists none by default, so a new role sees no tracked category until it is given one. In the
  clinic profiles, clinicians and nurses see all of them and the front desk sees none.
- **The reply says records are not shown, and not why.** Naming the category would disclose it. The
  sentence is added by the pipeline (`records_not_shown` in responses.yaml), not left to the model,
  and the model is told not to guess or call the record complete.
- **A tool's own answer is not shown in part.** The medication list, the medication review and the
  chart brief write their answer from every record they read. If one is withheld, the gateway drops
  that text and the person gets a fixed reply (`records_withheld`). A medication review that silently
  leaves out a medication is worse than none.
- **A category can be kept in the building.** The router is told the categories of the question and
  of the notes. One listed under `never_external` (by default all of them) keeps the request on the
  local model: decided in policy (`routing.rego`), and checked again in code whatever the policy
  answers.
- **The audit records categories and counts, never values:** on `audit.tool_calls`, how many records
  were in each category and how many were withheld; on `audit.routing`, the categories of the
  request. No new topic.

**Step 3 (not built): a text model.** A small multi-label classifier for the wording the lists
cannot know, trained with the loop of ADR 037 and added through `Detector(model=...)`. It is worth
building only if it beats the lists on text neither it nor its author has seen.

## First measurement
The lists alone, on the development set (`eval/results/sensitivity_dev_v1_lists_0ee7147255df.md`):

| | Caught |
|---|---|
| Text that names the category | 42 of 42 |
| Text that only implies it | 0 of 28 |
| Coded FHIR resources (code, medication, label or text) | 15 of 15 |
| Text that looks sensitive and is not, marked anyway | 7 of 24 |
| Everyday text and resources, marked anyway | 0 of 17 |

- **42 of 42 flatters the lists.** The same person wrote the rows and the lists within the hour. The
  rows came first and the lists were not changed after the run, but they share a vocabulary.
- **0 of 28 is the finding.** "Finishes a fifth of vodka most nights", "his counts have been good
  since 2019" and "she walked into a door again" contain no word a list can hold. This is what step 3
  is for.
- **The seven false marks are of three kinds:** a negated finding ("no history of depression"), a
  question about policy that names no person ("our cancellation policy for psychiatry"), and a word
  used loosely ("addicted to crossword puzzles"). Lists cannot tell these apart either.

## Consequences
- **What changes for a user, in the clinic profiles:** a front-desk user no longer sees a visit or a
  record that shows a tracked category, and is told some records are not shown. Clinicians and
  nurses see what they saw before. A question about a tracked category stays on the local model even
  when it names no one.
- **Policy pages are not records.** Only role-filtered tools are marked, so a knowledge-base page
  about HIV testing is still shown to everyone.
- **The lists decide what is hidden, and the lists are weak on implied wording** (0 of 28 in the
  first measurement). A record that implies a category without naming or coding it is shown. Until
  step 3, this layer is a floor, not a guarantee.
- **A direct tool's answer is all or nothing.** A nurse without a category would get no medication
  list at all for a patient with one protected medication. The tools could instead build their
  answer from the records the role may see; that is not built.
- **The record tools reading a database** (clinic-assistant) send no coded evidence: the database
  has no security labels or codes, so those records are marked by their text only.
- **The lists are not a clinical value set.** They were written by hand to exercise the code. A
  deployment replaces `privacy/sensitivity.yaml` with its compliance team's value sets
  (`sensitivity.load(path)`), and the file is validated on load.
- **Which records a law covers is a legal question the code does not answer.** Part 2 applies to
  the records of covered programs, not to every mention of a substance. The detector marks
  candidates; the hospital's policy decides what a mark means.
- **Marking too much is the safe direction, and it has a cost.** A medication with two uses
  (methadone for pain) marks a record that may not be protected. In step 2 that hides it from some
  roles. The lists leave out names that usually mean something else, and say so beside each list.
- **SNOMED CT and RxNorm codes are not read.** Epic and Oracle Health send both. A resource coded
  only in SNOMED is caught by its display text or not at all. Reading them needs licensed value
  sets, which a deployment has and this repository does not.
- **English only.**
- **The OPA policies were changed and their tests extended, and must be run where OPA is installed**
  (`opa test policies/ -v`). The Python tests replace OPA with a fake.
- Not built here: step 3; a frozen test set from someone else's text; consent (a patient allowing a
  disclosure); break-the-glass access; labels written back to the EHR.
