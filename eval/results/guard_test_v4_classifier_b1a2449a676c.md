# Guard classifier on frozen test set v4

Model guard-modernbert-v3 `b1a2449a676c`, thresholds {'crisis': 0.3, 'decision': 0.2}, run 2026-10-12T22:06:01.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 150 | 3/30 | 1/48 | 9/72 | 0.914 | 2/72 |
| Seen profiles | 75 | 3/15 | 0/24 | 6/36 | 0.877 | 1/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 0/15 | 1/24 | 3/36 | 0.950 | 1/36 |
|   housing-office | 25 | 0/5 | 0/8 | 2/12 | 0.920 | 1/12 |
|   legal-aid | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |
|   veterans-services | 25 | 0/5 | 0/8 | 1/12 | 0.966 | 0/12 |

Time per message: median 15.7 ms, 95th percentile 22.7 ms.
