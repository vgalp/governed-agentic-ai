# Baseline: rules+classifier on frozen test set v3

Config `b1a2449a676c`, run 2026-10-12T12:12:26.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 2/30 | 0/48 | 5/72 | 0.935 |
| Seen profiles | 75 | 2/15 | 0/24 | 5/36 | 0.888 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 0/15 | 0/24 | 0/36 | 0.982 |
|   housing-office | 25 | 0/5 | 0/8 | 0/12 | 1.000 |
|   legal-aid | 25 | 0/5 | 0/8 | 0/12 | 0.948 |
|   veterans-services | 25 | 0/5 | 0/8 | 0/12 | 1.000 |

Time per message: median 15.7 ms, 95th percentile 19.3 ms.
