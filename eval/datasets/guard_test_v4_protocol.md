# Guard classifier test set v4: protocol

The 150 messages were written before the v4 training data was generated and are unchanged since
then (the frozen file differs from the first draft only by a missing final newline). The SHA-256
below was recorded after the v4 classifier was trained, just before it was scored; the set was
committed together with the v4 training data and results. Same rules as v1
(`guard_test_protocol.md`); this file records why a fourth set exists and what is different.

## Why a fourth set
The v3 classifier was scored once on test set v3 (`eval/results/guard_test_v3_notes.md`) and then
put into the pipeline. A review run of 45 everyday ADHD questions (`eval/results/adhd_review_*`,
not a frozen test set) showed it giving the crisis reply to two ordinary messages: losing a wallet,
keys and phone, and feeling overwhelmed. On test set v3 it had also flagged everyday messages that
mention medication. v4 adds training data aimed at these mistakes:

- **allow:** losing or misplacing things, ordinary overwhelm about tasks, everyday possessions
  (selling, donating, decluttering), medication mentioned only in a routine or a reminder.
- **crisis:** goodbye signs phrased as the end of a plan, and short report-style crisis notes
  from staff.

Because v3's test results and the review run shaped v4, scoring v4 on test set v3 would overstate
it. Test sets v1 to v3 stay frozen and are reported as they are; v4 is measured on this new set, and
v3 and the baselines are scored on it too, so the comparison is like-for-like.

## Set
- `guard_test_v4.jsonl`: 150 messages, 25 per profile: 12 `allow`, 8 `decision`, 5 `crisis`.
- Profiles: the same six as v3. `adhd-assistant`, `clinic-assistant`, `county-benefits` are used in
  training; `housing-office`, `legal-aid` and `veterans-services` are never seen in training.
- Each profile's 12 `allow` messages include about four look-alikes: losing things, ordinary
  overwhelm, everyday possessions, or figures of speech. Some `crisis` messages use the same
  everyday words (keys, giving things away) in a dangerous sense.
- About 20% of messages are in Spanish or French.
- 100 messages test a hard-case rule from `docs/classifier/labels.md` (field `rule`).
- Policies: `guard_test_v4_policies.yaml`, frozen with the set; all six identical to v3.
- Synthetic: no real people or cases. Names are invented, and no surname is reused from any earlier
  test set or the review questions.

## Who wrote it
Drafted with an AI assistant (Claude), which had also seen v3's mistakes and the review run, and
helped write the training-data prompts. Drafts close to an earlier test or review message were
reworded; the highest remaining similarity to any of them is 0.78. The v4 training data did not exist
yet. `classifier/clean.py` removed any training message too close to a test message (similarity 0.75
or more) or using a test-set name; a check after training found no training or dev message at 0.75
or above (highest 0.747) and no test-set name. Anyone reading the results should know that the
test sets, the training prompts and the classifier share an author.

Reviewed before freezing by: Wladimir Galperin (vgalperin)

## Rules
As in v1: frozen by SHA-256 below; never used for training, tuning, threshold choice or prompt
changes; a message found wrong after freezing is reported as excluded, not changed; each system is
scored once per release on the whole set.

## Measures
As in v1, with the three unseen profiles reported together and separately. Wrong blocks are also
reported by kind (crisis reply or decision reply), since a crisis reply to an everyday message is
the mistake v4 is meant to reduce.

## Freeze
- `guard_test_v4.jsonl` SHA-256: 61f9a7d3996eedfaf8a22b1012090f6e7f735b587ab7fe7b8abed2b29f6d9635
- `guard_test_v4_policies.yaml` SHA-256: d1bff202b7e9496d39eccec581912bc4be9b3b899657a8cd1165c7cfcdf6165c