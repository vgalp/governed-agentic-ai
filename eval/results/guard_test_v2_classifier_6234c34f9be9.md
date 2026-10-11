# Guard classifier on frozen test set v2

Model guard-modernbert `6234c34f9be9`, thresholds {'crisis': 0.2, 'decision': 0.05}, run 2026-10-10T23:48:45.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 2/30 | 6/45 | 11/75 | 0.883 |
| Seen profiles | 90 | 2/18 | 2/27 | 10/45 | 0.860 |
| Unseen profiles (housing-office, legal-aid) | 60 | 0/12 | 4/18 | 1/30 | 0.918 |
|   housing-office | 30 | 0/6 | 2/9 | 0/15 | 0.938 |
|   legal-aid | 30 | 0/6 | 2/9 | 1/15 | 0.900 |

Time per message: median 15.5 ms, 95th percentile 18.1 ms.
