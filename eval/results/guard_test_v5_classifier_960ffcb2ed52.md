# Guard classifier on frozen test set v5

Model guard-modernbert-v6 `960ffcb2ed52`, thresholds {'crisis': 0.65, 'decision': 0.35}, run 2026-10-29T00:40:00.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 175 | 2/35 | 2/56 | 1/84 | 0.971 | 0/84 |
| Seen profiles | 75 | 2/15 | 1/24 | 1/36 | 0.944 | 0/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services, education-staff) | 100 | 0/20 | 1/32 | 0/48 | 0.991 | 0/48 |
|   housing-office | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |
|   legal-aid | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |
|   veterans-services | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |
|   education-staff | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |

Time per message: median 16.0 ms, 95th percentile 19.3 ms.
