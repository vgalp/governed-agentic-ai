# Guard classifier on the frozen test set

Model `6234c34f9be9`, thresholds {'crisis': 0.2, 'decision': 0.05}, run 2026-10-10T22:15:23.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 300 | 2/60 | 7/88 | 13/152 | 0.932 |
| Seen profiles | 225 | 2/45 | 3/66 | 12/114 | 0.930 |
| Unseen profile (housing-office) | 75 | 0/15 | 4/22 | 1/38 | 0.938 |

Time per message: median 15.4 ms, 95th percentile 18.1 ms.
