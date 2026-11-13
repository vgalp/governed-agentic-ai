# 034: gaai new, building an office profile from set questions

Status: Accepted · Date: 2026-11-12

## Context
The platform's claim is that a new office adopts it by configuration, not code. In practice that
meant copying a profile folder and editing seven YAML and Markdown files by hand: patterns,
responses, the classifier's policy, the prompt, the notes. That is the step an office cannot do
alone, and the one an evaluator asks to see.

## Decision
- **`gaai new` (and `/new` in the console) asks set questions:** the office's name and what it does;
  staff or public; each decision the assistant must never make, who makes it, short phrases people
  use to ask for it and one full question; a crisis contact; the office's policies or FAQ (pasted,
  or a file); web search yes or no. Office assistants without records only, in this version.
- **The local model drafts, the operator's answers stay.** With Ollama running, Qwen adds more
  phrasings per decision, the refusal wording, the classifier's description, example questions,
  and splits the FAQ into notes, as JSON matching a schema. Any failure falls back to the answers.
- **Built from `general-assistant`, keeping its safety baseline** (crisis, warning signs, abuse,
  medication, diagnosis, legal and financial decisions). Each office decision becomes its own rule
  and reply, checked before the general ones. For generated profiles the crisis terms add wordings
  the template misses: "ending my life", "don't want to live", and words staff relay about someone
  else ("wants to kill himself", "hurting herself").
- **Nothing generated is a regular expression.** Phrases are matched as written (whole words, any
  spacing, any case); a phrase with symbols is dropped.
- **Tested before it is saved:** the platform's own loader must accept the folder; three crisis
  messages (one first-person, two relayed) must be handed over; each decision's test questions must
  be refused; a question about each FAQ topic must get through. A decision that is not refused can
  be fixed on the spot by adding phrases. Failing profiles are saved only if the operator types
  `save`; passing ones on `y`. An existing profile is never overwritten. The new profile is not
  started: the operator reviews the files (notes and replies are marked as drafts), then `/use`.

## Consequences
- An office can get a working, tested assistant in minutes, and the files it produces are ordinary
  profile files, reviewed and changed like any other.
- The deterministic checks are the first net only: the guard classifier reads the generated
  description of the office's decisions as a second net.
- Building the first one showed that `general-assistant`'s crisis terms miss relayed and some
  first-person wordings. Generated profiles add them; the template itself is unchanged here, since
  its evaluations would need re-running (a follow-up).
- Records offices (a database or FHIR record system) are not generated; they still start from
  `clinic-*` or `county-benefits`.
