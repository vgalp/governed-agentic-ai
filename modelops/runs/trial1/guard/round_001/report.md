# Guard loop, round 1: guard-loop-trial1-r001

| Measure (gate sets) | Value | Target met |
|---|---|---|
| Crisis recall | 0.907 | False |
| False crisis | 0.034 | True |
| Decision let through | 0.119 | False |
| Dev macro F1 | 0.956 | True |

Gate messages: 173. Score 4.6182.

## Data

- written: 599
- agreed: 359
- disputed: 240
- rejected: 0
- trained: 354
- gate: 173
- dropped: {'too close to a held-out message': 3, 'test-set name': 2}
- gate_dropped: {'test-set name': 4}
- audit_queue: 275

Frontier: 136 calls, 42 from cache, 0 unreadable; $13.98 spent so far.

## Weakest cells (next round writes more here)

- adhd-assistant|joking_tone: 1/2 wrong
- adhd-assistant|code_switch: 1/2 wrong
- clinic-assistant|buried: 2/4 wrong
- clinic-assistant|soft_command: 3/9 wrong
- clinic-assistant|record_sensitive: 2/8 wrong
- clinic-assistant|low_mood: 2/10 wrong
- county-benefits|override: 1/5 wrong
- county-benefits|indirect: 2/10 wrong

These are gate-set numbers, written by the same frontier model as the training data. They decide when the loop stops, not whether the guard is better: that is the sealed benchmarks' job (`python -m modelops promote`).
