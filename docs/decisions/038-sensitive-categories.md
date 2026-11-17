# 038: Sensitive categories of health information

Status: Accepted · Date: 2026-11-14 · Steps 1 and 2 of 3 built (detection, and enforcement in the four clinic profiles). Step 3: a local language model and a trained classifier can be measured as the text reader; neither is used by the pipeline

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

**Step 3 (begun): a text model.** For the wording the lists cannot know.

- **Built: a local language model as the reader, for measuring** (`privacy/sensitivity_model.py`,
  `eval/run_sensitivity.py --model qwen2.5:7b`). It is given each category's one-sentence
  description from the value sets and four rules: implied wording counts; the text must be about a
  particular person; a denied finding does not count; a word in another sense does not count. It
  answers with category names. Local only: this is the text the rules keep in the building.
- **It is not wired into the pipeline.** A call takes about a second, which suits a question (one
  call, for routing) and not a chart (a call per record). Its numbers on the development set decide
  the next move:
  - good on implied wording, few false marks: use it for questions, and train a small classifier
    from its labels for records (the loop of ADR 037);
  - poor: the classifier needs frontier-written data from the start, as the guard did.
- **Measured (`eval/results/sensitivity_dev_v1_*qwen2.5-7b*`):** alone it caught 18 of 28 implied texts
  and 38 of 42 explicit ones, and marked 5 of 24 look-alikes; with the lists, 42 of 42, 18 of 28 and 10
  of 24. Better than the lists on implied wording, not good enough to hide records, and too slow for
  them. So the classifier gets frontier-written data from the start.
- **Built: the classifier's data and trainer** (`python -m modelops sensitivity data | train`,
  `modelops/sensitivity_data.py`, `modelops/sensitivity_train.py`).
  - **Data.** One model writes short texts for a cell (a category; explicit, implied or look-alike; an
    angle), another labels them without seeing what they were written for, and only texts where both
    agree are kept. A text written as implied that the lists already catch is filed as explicit.
    Anything close to the hand-written development set is dropped.
  - **Test set.** The two models swap: the reviewer model writes, the frontier model labels. Training
    text close to it is dropped. No model is tested on its own writing, but all of it is model
    writing.
  - **Model.** ModernBERT-base, one yes-or-no output per category, reading the text alone. One
    threshold per category, chosen on held-back training text under a limit on false marks.
  - **Measured the same way as the lists and Qwen** (`eval/run_sensitivity.py --classifier`), on the
    development set and on the run's test set.
- **First trained version, run `sens1`** (2,615 training texts; results in `eval/results/sensitivity_*sens1*`):
  - On the run's test set (725 texts another model wrote): implied wording caught 144 of 169 (Qwen 145,
    lists 0); look-alikes marked 11 of 270 (Qwen 31, lists 73); 0.036 s a text (Qwen 0.37 s).
  - On the hand-written development set: implied 16 of 28 (Qwen 18), explicit 32 of 42 (Qwen 38),
    look-alikes marked 4 of 24 (Qwen 5), everyday text marked 2 of 10.
  - So: as good as Qwen and ten times faster on model-written text, a little behind it on text a
    person wrote, and it marks too much everyday text. The model-written test set flatters it.
- **Built: a test set from text people wrote** (`python -m modelops sensitivity mine --source ...`,
  `modelops/sensitivity_mine.py`). Sentences from published patient descriptions (NCBI Open-Patients),
  some from descriptions chosen at random and some from descriptions that mention a category. Two
  frontier models label each blind; a sentence is kept when they agree. The detectors take no part
  in choosing sentences. The text stays outside the repository, under its own licence; the run keeps
  a manifest of ids, labels and hashes.
- **On that set, the first version does not hold up** (1,509 sentences; `eval/results/sensitivity_human_test_v1_*`):

  | | Lists | Qwen alone | Classifier `sens1` alone |
  |---|---|---|---|
  | Categories the lists catch (165) | 165 | 149 | 101 |
  | Categories the lists miss (128) | 11 | 75 | 76 |
  | Sentences beside a sensitive topic, marked anyway | 82 of 674 | 57 | 149 |
  | Ordinary sentences, marked anyway | 5 of 563 | 13 | 83 |
  | Rows exactly right | 1,307 | 1,365 | 1,162 |

  The classifier finds what Qwen finds at an eighth of the time, and marks seven times as many ordinary
  sentences, most of them as genetics. It had only seen short notes and messages written by a model.
- **Built: training text from what people wrote** (`sensitivity mine --purpose train`, then
  `sensitivity train --source ... --tag r2`). Sentences from descriptions the test set did not use.
  The cheaper model labels all of them; the stronger one confirms every sentence given a category and
  a sample of the rest, and the sample's disagreements are reported. Training and validation never
  share a description. The model is trained on the generated and the human text together, under a
  new name, so the first is kept for comparison.
- **Second version, `sens1-r2`** (11,293 training texts: 2,615 generated and 8,678 human-written; results
  in `eval/results/sensitivity_*sens1-r2*`). Classifier alone, against the first version and Qwen:

  | | First version | Qwen | `sens1-r2` |
  |---|---|---|---|
  | Human-written test set: categories the lists miss, caught (128) | 76 | 75 | 88 |
  | Human-written test set: ordinary sentences marked anyway (563) | 83 | 13 | 7 |
  | Human-written test set: sentences beside a sensitive topic marked anyway (674) | 149 | 57 | 16 |
  | Human-written test set: rows exactly right (1,509) | 1,162 | 1,365 | 1,426 |
  | Generated test set: implied wording caught (169) | 144 | 145 | 151 |
  | Generated test set: look-alikes marked anyway (270) | 11 | 31 | 5 |
  | Hand-written development set: implied wording caught (28) | 16 | 18 | 14 |
  | Hand-written development set: look-alikes marked anyway (24) | 4 | 5 | 2 |
  | Seconds a text | 0.036 | 0.29 to 0.37 | 0.036 |

  What this does and does not show:
  - It is the best of the three on the two larger sets and ten times faster than Qwen.
  - On the hand-written set it is still behind Qwen on implied wording (14 against 18 of 28).
  - The human-written test set is now text from the same source as part of its training (other
    descriptions, never the same ones). It is no longer an independent check of this model: a set
    from another source is needed for that.
  - Weak spots: implied HIV (1 of 7) and implied sexual health (0 of 6) on the human-written set.
  - With the lists added, every named category is caught and the lists' own false marks come back
    (94 of 674 beside a sensitive topic): the lists cannot tell a negated or general mention.
  - Its training text includes sentences under share-alike and non-commercial terms. That has to be
    settled before the model is published or sold.
- **A profile can turn the classifier on; none does.** `sensitivity.text_model: <folder in models/>` in
  profile.yaml makes the gateway, the router's check of the question and the pipeline read text with the
  lists and that model. The model adds categories; it never removes one the lists, a code or a label
  found.
  - A chart's records are read in one pass of the model, not one call each.
  - The audit names the model on every tool call it read (`sensitive_text_model`).
  - If the named model cannot be loaded, the request fails. It does not fall back to the lists alone:
    an office that turned the model on must not get less without being told.
  - The record tools keep to labels, codes and lists; the gateway reads the text.
  - **Observe is the default mode** (`text_model_mode: observe`). The model reads the records, the audit
    records what it alone would have marked (`model_would_mark`) and how many records the role would
    have lost (`model_would_withhold`), and nothing is marked or withheld on its word. `enforce` makes
    it act. This exists because its first enforced run in the gateway withheld what appears to be a
    patient-details record: evidence from real use should come before trust.
  - It is off in every profile in this repository. It is for demonstration on synthetic records until
    the model has been scored on labels a person reviewed.
- **Not built:** a frozen test set labelled by people. The mined set is people's text with models'
  labels: better evidence than model-written text, and still not a person's judgement. No model is
  turned on for enforcement on these numbers alone. Also not built: rounds (new data aimed
  at what the last model missed), and reading SNOMED CT or RxNorm.

## First measurement
The lists alone, on the development set (`eval/results/sensitivity_dev_v1_lists_b4f10f83f0ef.md`):

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
