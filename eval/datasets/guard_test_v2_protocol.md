# Guard classifier test set v2: protocol

Written and committed **before** the v2 classifier is trained. Same rules as v1
(`guard_test_protocol.md`); this file records why a second set exists and what is different.

## Why a second set
The v1 classifier was scored once on test set v1 (`eval/results/guard_test_classifier_6234c34f9be9.md`).
Its 22 mistakes were then read, and the v2 training data adds kinds of messages aimed at them
(record lookups that mention doses or denials, general questions about rules, orders to make a
decision, warning signs written as statements, abuse mentioned inside an ordinary request, more
Spanish and French). Because v1's test mistakes shaped v2, scoring v2 on test set v1 would
overstate it. Test set v1 stays frozen and is reported as it is; v2 is measured on this new set.

## Set
- `guard_test_v2.jsonl`: 150 messages, 30 per profile: 15 `allow`, 9 `decision`, 6 `crisis`.
- Profiles: `adhd-assistant`, `clinic-assistant`, `county-benefits` (used in training), and two
  profiles the classifier never sees in training: `housing-office` (also in v1) and `legal-aid`,
  a fictional legal aid intake office, new in v2, with a different kind of decision (legal
  advice, whether someone has a case).
- About 20% of messages are in Spanish or French.
- 94 messages test a hard-case rule from `docs/classifier/labels.md` (field `rule`).
- Policies: `guard_test_v2_policies.yaml`, frozen with the set. The first four are identical to v1.
- Synthetic: no real people, patients, clients or cases. Names are invented and differ from v1.

## Who wrote it
Drafted with an AI assistant (Claude), which had also seen v1's mistakes and helped write the
training-data prompts. The names and wording are new. Two drafts that were almost identical to v1
messages were reworded; overlap with the existing training data was measured but no message was
changed because of it, and the v2 training data did not exist yet. After freezing, `classifier/clean.py` removes any training message
too close to a test message (similarity 0.75 or more) or using a test-set name. Anyone reading the
results should know that the test sets, the training prompts and the classifier share an author.

Reviewed before freezing by: vgalperin

## Rules
As in v1: frozen by SHA-256 below; never used for training, tuning, threshold choice or prompt
changes; a message found wrong after freezing is reported as excluded, not changed; each system is
scored once per release on the whole set.

## Measures
As in v1, with the unseen profiles reported together and separately.

## Freeze
- `guard_test_v2.jsonl` SHA-256: `d474779cc7d6b7b8bc416d743ac7e4f8c703870478b5020b686dcabbd213c43c`
- `guard_test_v2_policies.yaml` SHA-256: `bfaf330cefe39e9ef5203e63aa8bd6f0b53749fc58434507c8bde09c21956e08`
