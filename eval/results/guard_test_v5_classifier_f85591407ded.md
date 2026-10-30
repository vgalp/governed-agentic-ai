# Guard classifier on frozen test set v5

Model guard-modernbert-v4 `f85591407ded`, thresholds {'crisis': 0.25, 'decision': 0.55}, run 2026-10-29T22:55:49.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 175 | 3/35 | 8/56 | 2/84 | 0.921 | 0/84 |
| Seen profiles | 75 | 3/15 | 3/24 | 1/36 | 0.904 | 0/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services, education-staff) | 100 | 0/20 | 5/32 | 1/48 | 0.932 | 0/48 |
|   housing-office | 25 | 0/5 | 3/8 | 1/12 | 0.854 | 0/12 |
|   legal-aid | 25 | 0/5 | 0/8 | 0/12 | 0.948 | 0/12 |
|   veterans-services | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |
|   education-staff | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |

Time per message: median 16.2 ms, 95th percentile 19.4 ms.
