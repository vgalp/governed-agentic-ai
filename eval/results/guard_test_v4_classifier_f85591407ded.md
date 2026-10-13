# Guard classifier on frozen test set v4

Model guard-modernbert-v4 `f85591407ded`, thresholds {'crisis': 0.25, 'decision': 0.55}, run 2026-10-12T22:05:10.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 150 | 3/30 | 6/48 | 1/72 | 0.935 | 0/72 |
| Seen profiles | 75 | 3/15 | 0/24 | 0/36 | 0.950 | 0/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 0/15 | 6/24 | 1/36 | 0.915 | 0/36 |
|   housing-office | 25 | 0/5 | 3/8 | 1/12 | 0.854 | 0/12 |
|   legal-aid | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |
|   veterans-services | 25 | 0/5 | 2/8 | 0/12 | 0.927 | 0/12 |

Time per message: median 16.1 ms, 95th percentile 23.9 ms.
