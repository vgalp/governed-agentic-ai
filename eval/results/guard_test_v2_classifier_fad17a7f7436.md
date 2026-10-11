# Guard classifier on frozen test set v2

Model guard-modernbert-v2 `fad17a7f7436`, thresholds {'crisis': 0.8, 'decision': 0.4}, run 2026-10-10T23:43:56.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 4/30 | 7/45 | 1/75 | 0.919 |
| Seen profiles | 90 | 4/18 | 2/27 | 1/45 | 0.915 |
| Unseen profiles (housing-office, legal-aid) | 60 | 0/12 | 5/18 | 0/30 | 0.921 |
|   housing-office | 30 | 0/6 | 2/9 | 0/15 | 0.938 |
|   legal-aid | 30 | 0/6 | 3/9 | 0/15 | 0.903 |

Time per message: median 15.5 ms, 95th percentile 18.1 ms.
