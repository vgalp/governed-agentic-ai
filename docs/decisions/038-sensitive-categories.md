# 038: Sensitive categories of health information

Status: Accepted · Date: 2026-11-14 · Step 1 of 3 built (detection and its measurement). Nothing is enforced yet

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

**Step 2 (not built): act on it.**
- Each record a tool returns carries its categories. The gateway drops the records of a category the
  role may not see, as it does for data classes now, with the decision made by OPA
  (`roles.<role>.sensitive_categories` in the profile).
- The reply says that some records are not shown for this role. It does not say which category:
  naming it would disclose it.
- A tool whose own text is the answer (the medication review) is not shown in part: with a record
  withheld, a fixed reply sends the person to a clinician. A medication review that silently leaves
  out a medication is worse than none.
- The router is told the categories of the question and of the notes. A category the profile lists
  under `never_external_categories` keeps the request on the local model, checked in policy and
  again in code.
- A new audit topic records the categories and kinds of evidence of every request, and what was
  withheld.

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
- **Nothing changes for a user yet.** Step 1 adds a module, a file of lists, a scorer and tests. No
  stage of the pipeline calls it.
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
- Not built here: steps 2 and 3; a frozen test set from someone else's text; labels written back to
  the EHR.
