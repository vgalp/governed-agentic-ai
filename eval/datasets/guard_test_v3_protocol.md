# Guard classifier test set v3: protocol

Written and committed **before** the v3 training data is generated and before the v3 classifier
is trained. Same rules as v1 (`guard_test_protocol.md`); this file records why a third set exists
and what is different.

## Why a third set
The v2 classifier was scored once on test set v2 (`eval/results/guard_test_v2_classifier_*.md`),
and so were three baselines (rules only, Llama Guard 3 8B, and the current pipeline). The v2
mistakes were then read (`eval/results/guard_test_v2_notes.md`). v3 changes two things because of
them:

- **Training data:** three new kinds of message: requests for a judgment about a person
  (`judgment`), indirect goodbye signs such as giving things away or writing farewell notes
  (`goodbye_signs`), and crisis messages in Spanish and French (`other_language`).
- **Threshold choice:** the crisis threshold is now the lowest one on dev that stays within a 2%
  false-alarm budget, instead of the highest one with the best dev score (`classifier/calibrate.py`).

Because v2's test mistakes shaped v3, scoring v3 on test set v2 would overstate it. Test sets v1
and v2 stay frozen and are reported as they are; v3 is measured on this new set. v2 and the
baselines are also scored on this set so the comparison is like-for-like.

## Set
- `guard_test_v3.jsonl`: 150 messages, 25 per profile: 12 `allow`, 8 `decision`, 5 `crisis`.
- Profiles: `adhd-assistant`, `clinic-assistant`, `county-benefits` (used in training), and three
  profiles the classifier never sees in training: `housing-office`, `legal-aid` (both also in v2)
  and `veterans-services`, a fictional county veterans service office, new in v3, with its own
  kinds of decision (benefit eligibility, disability ratings, appeals, treatment choice).
- About 20% of messages are in Spanish or French.
- 94 messages test a hard-case rule from `docs/classifier/labels.md` (field `rule`).
- Policies: `guard_test_v3_policies.yaml`, frozen with the set. The first five are identical to v2.
- Synthetic: no real people, patients, clients, veterans or cases. Names are invented and none is
  reused from v1 or v2.

## Who wrote it
Drafted with an AI assistant (Claude), which had also seen v2's mistakes and helped write the
training-data prompts. The names and wording are new. Four drafts that were close to v1 or v2
messages were reworded; the highest remaining similarity to any v1 or v2 message is 0.81. The v3
training data did not exist yet. After freezing, `classifier/clean.py` removes any training message
too close to a test message (similarity 0.75 or more) or using a test-set name. Anyone reading the
results should know that the test sets, the training prompts and the classifier share an author.

Reviewed before freezing by: vgalperin

## Rules
As in v1: frozen by SHA-256 below; never used for training, tuning, threshold choice or prompt
changes; a message found wrong after freezing is reported as excluded, not changed; each system is
scored once per release on the whole set.

## Measures
As in v1, with the three unseen profiles reported together and separately.

## Freeze
- `guard_test_v3.jsonl` SHA-256: 521a91f9ad89b44deda1f42004afd883fce285c29f8a6309df50deda5f138d6c
- `guard_test_v3_policies.yaml` SHA-256: 736fb653de173bff1a7b9c1211fbfabf26d815493675ea634b049dd9ab8cff96