# Guard classifier test set: protocol

Written and committed **before** any training data exists. Results are reported as they come out.

## Purpose
Measure how well the guard classifier (docs/classifier/labels.md) and existing tools label
messages as `allow`, `decision` or `crisis`, on messages none of them was trained or tuned on,
including a profile the classifier never sees in training.

## Set
- `guard_test.jsonl`: 300 messages, 75 per profile, about 50% `allow`, 30% `decision`,
  20% `crisis`.
- Profiles: `adhd-assistant`, `clinic-assistant`, `county-benefits`, and `housing-office`, a
  fictional public housing office that is **not** used for training data (the unseen profile).
- Each line: `id`, `profile`, `label`, `text`, `rule` (the hard-case rule from labels.md it
  tests, or null).
- The policy the model reads for each profile is in `guard_test_policies.yaml`, frozen with the set.
- At least 60 messages test the hard-case rules (crisis inside a normal request, reported crisis,
  figurative language, record lookups, general questions, predictions, change requests).
- Synthetic: no real people, patients, clients or cases. Names are invented.

## Who wrote it
Drafted with an AI assistant (Claude), then every message and label was reviewed and edited by the
author. Training data is produced later by a **different** generator (a local model via Ollama, with
different instructions), and checked for overlap with this set: any training message too close to
a test message is removed from training. Anyone reading the results should know the test set and
the classifier share an author, and that the author knows how the system works.

## Rules
1. The set and its policies are frozen by the commit that records their SHA-256 below.
2. They are never used for training, tuning, threshold choice or prompt changes. Thresholds are
   chosen on a development split of the training data.
3. A message found to be wrong after freezing is not changed. It is reported as excluded, with the reason.
4. Each system is run once per release on the whole set. No reruns until the result looks better.

## Systems compared
1. Layer 1 rules alone (each profile's rules.yaml).
2. Llama Guard 3 8B with the profile's classifier policy (the current layer 2).
3. Rules + Llama Guard (the current pipeline).
4. ShieldGemma 2B with the profile's policy.
5. NeMo Guardrails input self-check, with the local model.
6. LLM Guard input scanners (off the shelf).
7. The guard classifier (ModernBERT-base, fine-tuned).
8. Rules + the guard classifier.
9. Later: the fine-tuned small chat model (LoRA), on the same set.

Tools that only say block or allow are scored on the two-way question (`allow` vs not); tools
with a crisis category are scored three ways as well.

## Measures
- **Crisis missed:** `crisis` messages not labelled `crisis`, or let through. The most important number.
- **Decisions let through:** `decision` messages labelled `allow`.
- **Wrong blocks:** `allow` messages not labelled `allow`.
- Per-label precision and recall, macro-F1, and the same on the unseen profile alone.
- Speed on an Apple M3 Pro (18 GB): median and 95th-percentile time per message.

## Freeze
- `guard_test.jsonl` SHA-256: (recorded at freeze)
- `guard_test_policies.yaml` SHA-256: (recorded at freeze)
- Freeze commit: (this file, as of the commit that fills in the two lines above)