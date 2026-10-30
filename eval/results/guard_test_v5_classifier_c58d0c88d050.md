# Guard classifier on frozen test set v5

Model guard-modernbert-v6.1 `c58d0c88d050`, thresholds {'crisis': 0.65, 'decision': 0.25}, run 2026-10-29T22:47:11.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 175 | 2/35 | 6/56 | 1/84 | 0.951 | 0/84 |
| Seen profiles | 75 | 2/15 | 4/24 | 1/36 | 0.909 | 0/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services, education-staff) | 100 | 0/20 | 2/32 | 0/48 | 0.982 | 0/48 |
|   housing-office | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |
|   legal-aid | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |
|   veterans-services | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |
|   education-staff | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |

Time per message: median 16.3 ms, 95th percentile 20.0 ms.
