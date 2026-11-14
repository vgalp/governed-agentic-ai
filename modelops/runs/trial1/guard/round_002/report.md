# Guard loop, round 2: guard-loop-trial1-r002

| Measure (gate sets) | Value | Target met |
|---|---|---|
| Crisis recall | 0.984 | True |
| False crisis | 0.069 | False |
| Decision let through | 0.050 | True |
| Dev macro F1 | 0.914 | False |

Gate messages: 209. Score 4.7639.

## Data

- written: 597
- agreed: 358
- disputed: 238
- settled_by_reviewer: 206
- rejected_by_reviewer: 0
- sample_doubted: 0
- rejected: 1
- trained: 556
- gate: 36
- dropped: {'test-set name': 7, 'too close to a held-out message': 1}
- gate_dropped: {'too close to a held-out message': 236, 'test-set name': 9}
- audit_queue: 32

Frontier: 115 calls, 63 from cache, 0 unreadable, 0 declined ; $29.75 spent so far.

## Weakest cells (next round writes more here)

- adhd-assistant|low_mood: 6/10 wrong
- clinic-assistant|soft_command: 3/9 wrong
- clinic-assistant|low_mood: 2/10 wrong
- county-benefits|override: 1/5 wrong
- clinic-assistant|record_sensitive: 1/8 wrong
- county-benefits|low_mood: 1/9 wrong
- county-benefits|indirect: 1/10 wrong
- county-benefits|record_sensitive: 1/10 wrong

These are gate-set numbers, written by the same frontier model as the training data. They decide when the loop stops, not whether the guard is better: that is the sealed benchmarks' job (`python -m modelops promote`).
