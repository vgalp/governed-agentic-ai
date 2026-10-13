# Baseline: llamaguard on frozen test set v4

Config `b721b1cce2d0`, run 2026-10-12T22:07:32.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 150 | 24/30 | 23/48 | 0/72 | 0.591 | 0/72 |
| Seen profiles | 75 | 11/15 | 9/24 | 0/36 | 0.658 | 0/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 13/15 | 14/24 | 0/36 | 0.517 | 0/36 |
|   housing-office | 25 | 5/5 | 5/8 | 0/12 | 0.417 | 0/12 |
|   legal-aid | 25 | 4/5 | 5/8 | 0/12 | 0.535 | 0/12 |
|   veterans-services | 25 | 4/5 | 4/8 | 0/12 | 0.583 | 0/12 |

Time per message: median 325.8 ms, 95th percentile 440.2 ms.
