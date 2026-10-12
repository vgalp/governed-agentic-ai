# Baseline: rules on frozen test set v3

Config `b721b1cce2d0`, run 2026-10-12T12:12:42.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 30/30 | 46/48 | 1/72 | 0.243 |
| Seen profiles | 75 | 15/15 | 22/24 | 1/36 | 0.266 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 15/15 | 24/24 | 0/36 | 0.216 |
|   housing-office | 25 | 5/5 | 8/8 | 0/12 | 0.216 |
|   legal-aid | 25 | 5/5 | 8/8 | 0/12 | 0.216 |
|   veterans-services | 25 | 5/5 | 8/8 | 0/12 | 0.216 |

Time per message: median 0.0 ms, 95th percentile 0.0 ms.
