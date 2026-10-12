# Guard classifier on frozen test set v3

Model guard-modernbert-v3 `b1a2449a676c`, thresholds {'crisis': 0.3, 'decision': 0.2}, run 2026-10-12T12:11:28.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 1/30 | 0/48 | 5/72 | 0.944 |
| Seen profiles | 75 | 1/15 | 0/24 | 5/36 | 0.906 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 0/15 | 0/24 | 0/36 | 0.982 |
|   housing-office | 25 | 0/5 | 0/8 | 0/12 | 1.000 |
|   legal-aid | 25 | 0/5 | 0/8 | 0/12 | 0.948 |
|   veterans-services | 25 | 0/5 | 0/8 | 0/12 | 1.000 |

Time per message: median 16.1 ms, 95th percentile 19.6 ms.
