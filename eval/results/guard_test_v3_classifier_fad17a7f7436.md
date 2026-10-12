# Guard classifier on frozen test set v3

Model guard-modernbert-v2 `fad17a7f7436`, thresholds {'crisis': 0.8, 'decision': 0.4}, run 2026-10-12T12:11:52.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 3/30 | 9/48 | 1/72 | 0.907 |
| Seen profiles | 75 | 2/15 | 2/24 | 1/36 | 0.914 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 1/15 | 7/24 | 0/36 | 0.898 |
|   housing-office | 25 | 0/5 | 4/8 | 0/12 | 0.841 |
|   legal-aid | 25 | 1/5 | 2/8 | 0/12 | 0.878 |
|   veterans-services | 25 | 0/5 | 1/8 | 0/12 | 0.964 |

Time per message: median 16.6 ms, 95th percentile 20.6 ms.
