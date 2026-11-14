# Sensitive categories: development set v1

126 synthetic rows, written 2026-11-14 for ADR 038: 104 texts in `sensitivity_dev_v1.jsonl` and 22 FHIR
resources in `sensitivity_dev_v1_resources.json`. (The resources are not a `.jsonl` file: every
`.jsonl` here is read as text the guard classifier must stay away from in training.) No real person's
record or message is in it; the names are invented.

**It is a development set.** It may be rerun and its mistakes may be read. It is not a frozen test
set, and no number from it is evidence about real records.

## Rows

| Stratum | Rows | What it is |
|---|---|---|
| `explicit` | 42 | Text that names the category in ordinary clinical words (six per category) |
| `implicit` | 28 | Text that implies the category without naming it (four per category) |
| `hard_negative` | 24 | Text that looks sensitive and is not: other meanings of a word, a negated finding, a question about policy and no person |
| `plain` | 10 | Everyday clinic text |
| `resource` | 15 | FHIR resources with a category: by ICD-10-CM code, medication, security label or text |
| `resource_negative` | 7 | FHIR resources with none, including codes left out on purpose (nicotine, weeks of gestation) |

Seven categories have text rows: substance use, mental health, HIV, sexually transmitted infections,
reproductive health, abuse and violence, genetics. The eighth, "marked restricted by the source
system", exists only as a label on a resource.

Each row: `id`, `kind` (`text` or `resource`), `stratum`, `categories` (what it should get) and the
text or the resource. Condition resources carry a neutral display ("Condition 3"), so the code alone
is tested.

## Limits

- **One author wrote the rows and the lists, in the same hour.** The rows were written first and the
  lists were not changed after the first run, but the explicit rows use the words their author would
  put in a list. Expect the lists to do worse on other people's wording.
- **The labels are one person's judgment** and nobody reviewed them. Three choices that could be
  argued the other way: a negated finding ("no history of depression") is labelled as no category;
  a question about policy that names no person is labelled as no category; a medication with other
  common uses is left out of the lists.
- **English only**, and short. Real notes are long, abbreviated and misspelled.
- A frozen test set should come from someone else's text, and should be built before any trained
  model is compared with the lists for a decision.

## Scoring

`uv run python -m eval.run_sensitivity`. Counts, not rates: per category, rows caught by stratum and
rows marked without it; then rows with no category that were marked at all.
