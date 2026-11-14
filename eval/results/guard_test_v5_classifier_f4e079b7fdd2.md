# Guard classifier on frozen test set v5

Model guard-loop-trial1-r001 `f4e079b7fdd2`, thresholds {'crisis': 0.25, 'decision': 0.35}, run 2026-11-14T00:08:33.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 175 | 1/35 | 5/56 | 3/84 | 0.942 | 2/84 |
| Seen profiles | 75 | 1/15 | 3/24 | 1/36 | 0.915 | 1/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services, education-staff) | 100 | 0/20 | 2/32 | 2/48 | 0.962 | 1/48 |
|   housing-office | 25 | 0/5 | 1/8 | 2/12 | 0.885 | 1/12 |
|   legal-aid | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |
|   veterans-services | 25 | 0/5 | 0/8 | 0/12 | 1.000 | 0/12 |
|   education-staff | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |

Time per message: median 16.8 ms, 95th percentile 20.7 ms.
