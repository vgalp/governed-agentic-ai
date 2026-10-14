# Guard classifier test set v5: protocol

Written, reviewed and frozen (SHA-256 below) **before** any v5 training data is generated, before
the v5 classifier is trained, and before any rule change proposed by the rule-review agent. The
freeze is committed and pushed on its own, before that work starts. Same rules as v1
(`guard_test_protocol.md`); this file records why a fifth set exists and what is different.

## Why a fifth set
Test set v4 and the ADHD review run shaped v5 (`eval/results/guard_test_v4_notes.md`), so scoring v5
on v4 would overstate it. v4's mistakes: three crisis messages that hide a warning sign inside an
ordinary planning request; "I just freeze" read as crisis (the word never appeared in training);
commands read as allowed ("Go ahead and end the lease"); a medication-timing question let through.
Set v5 tests those kinds of message with new wording, and adds a seventh profile.

v5 is also the set on which the rule-review agent's approved rule changes are measured: the rules,
rules + classifier and pipeline are scored once before and once after the change.

## Set
- `guard_test_v5.jsonl`: 175 messages, 25 per profile: 12 `allow`, 8 `decision`, 5 `crisis`.
- Profiles: the six of v4, plus **education-staff** (staff at a fictional K-12 school district).
  `adhd-assistant`, `clinic-assistant` and `county-benefits` are used in training;
  `housing-office`, `legal-aid`, `veterans-services` and `education-staff` are never seen in
  training. education-staff is also a working profile, whose guard policy lives in its own
  `profile.yaml`, not in `classifier/policies.yaml`, so it can never enter the training data.
- Kinds of message emphasized:
  - **allow:** "freeze", "lock up", "paralysed", "shut down" and "everything on my list" in an
    ordinary sense; losing things; giving things away before a move; medication only in a routine.
  - **decision:** commands ("Go ahead and…", "Just mark…", "Terminate…", "Change the grade…");
    school decisions (suspension, special-education eligibility, grades, placement).
  - **crisis:** warning signs inside an ordinary request (letters written for the kids, pills saved
    in a drawer, a pet given away, "won't be around"); abuse disclosures; a student's self-harm,
    abuse, neglect or a weapon at school. One matched pair: the same "overwhelmed, I just freeze"
    wording with and without a danger sign.
- About 20% of messages are in Spanish or French.
- 120 messages test a hard-case rule from `docs/classifier/labels.md` (field `rule`).
- Policies: `guard_test_v5_policies.yaml`, frozen with the set; the first six identical to v4.
- Synthetic: no real people or cases. Names are invented; no name appears in an earlier test set,
  the review questions, or the current training data.

## Who wrote it
Drafted with an AI assistant (Claude), which had also seen v4's mistakes and the review run, and
helped write the training-data prompts. Drafts close to an earlier test set, the review questions
or the red-team sets were reworded; the highest remaining similarity to any of them is 0.74, and to
any current training or dev message 0.73. The v5 training data did not exist yet.
`classifier/clean.py` will remove any v5 training message too close to a test message (similarity
0.75 or more) or using a test-set name. Anyone reading the results should know that the test sets,
the training prompts and the classifier share an author.

Timeline note: self-improvement round 1 (`classifier/improve.py`, 2026-10-13, attacking v4) was
generated before this freeze, when v5 was not yet in the repository, so its attack messages could not
be checked against v5 at the time. None of its messages enter training before `classifier/clean.py`
has dropped anything close to a v5 message. The set frozen below is unchanged from the draft
delivered for review before round 1 ran (same SHA-256).

Correction (2026-10-14): the freeze was pushed in commit 0918b87 together with other work, not on
its own: the input agent and its dev runs, the self-improvement agent, and round 1's attack messages
and review queue (attack messages, not training data). The v5 files in that commit are unchanged
from the reviewed draft (SHA-256 below). No v5 training data existed yet, and none of round 1's
messages had entered training.

Reviewed before freezing by: Wladimir Galperin (vgalperin), 2026-10-14, no changes

## Rules
As in v1: frozen by SHA-256 below; never used for training, tuning, threshold choice, prompt
changes or rule changes; a message found wrong after freezing is reported as excluded, not changed;
each system is scored once per release on the whole set.

## Measures
As in v4: the four unseen profiles reported together and separately; wrong blocks also reported by
kind (crisis reply or decision reply).

## Freeze
- `guard_test_v5.jsonl` SHA-256: 5d5fcae83ccfec37edef3f37167440b85a8458723d2c008aae4dd292104c089b
- `guard_test_v5_policies.yaml` SHA-256: 10525bf591c9023328d1c2356f8ebd6fc80cad0b88d5b72b2e30c8e229ea7e8b