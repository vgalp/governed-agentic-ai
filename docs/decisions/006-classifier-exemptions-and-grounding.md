# 006: Narrow classifier exemptions and a grounding check

Status: Accepted · Date: 2026-10-07

## Context
In the clinic profile the safety classifier (Llama Guard 3 8B) flagged plain record lookups
such as "What medications is Chester Aufderhar taking?" as medical advice (S2) when it saw
only the question, so staff could not read a medication list. Rewording the classifier
policy did not change this. With the answer in view, the classifier judged lookups correctly.

The development probe (`eval/classifier_probe.py`, 2026-10-07) compared options:
- **S2 on the question blocks** (original): 10/12; every medication lookup blocked.
- **S2 on the question never blocks; check the answer only**: 8/12. Answers to unsafe
  questions got through with clinical decision criteria, an invented source ("the clinic's
  approved guidelines"), a citation to an unrelated policy (kb-cl-001, clinic hours, for a
  metformin question) and invented fill-in fields such as `[METFORMIN_DOSE]`.

## Decision
- **Narrow exemptions, not deferral.** A profile may list `classifier_exemptions` in
  `rules.yaml`. An input classifier flag is overridden only when the question matches the
  exemption and contains none of its cancelling words. The clinic's single exemption: a
  record lookup ("what/list/show … medications", "current medications") with no decision
  words (should, increase, double, prescribe, need to, best, safe, give …).
- The loader refuses exemptions for crisis, and exemptions without cancelling words.
- Overrides are audited (`override` on the guardrail event) and shown on the dashboard.
- The answer to an exempted question still goes through every output check.
- **Invented fill-in fields** are blocked by an output rule: a bracketed name without a
  number (`[METFORMIN_DOSE]`); real masking tokens always end in a number (`[PERSON_1]`).
- **Grounding check.** For profiles that cite sources, an answer may only cite IDs of
  notes the model was given (`source_id_pattern`). Anything else is blocked as `ungrounded`.

## Consequences
- Staff can read what records say; requests for decisions are still blocked before the model.
- The grounding check is a first, simple version of layer 3: it catches citations to
  sources that were not provided, not claims that misstate a provided source.
- The exemption and the decision words were tuned on development prompts. They must be
  measured on a frozen clinic red-team set before results are reported.