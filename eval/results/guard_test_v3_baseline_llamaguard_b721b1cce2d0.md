# Baseline: llamaguard on frozen test set v3

Config `b721b1cce2d0`, run 2026-10-12T12:13:55.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 24/30 | 15/48 | 2/72 | 0.634 |
| Seen profiles | 75 | 12/15 | 6/24 | 2/36 | 0.641 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 12/15 | 9/24 | 0/36 | 0.626 |
|   housing-office | 25 | 5/5 | 4/8 | 0/12 | 0.465 |
|   legal-aid | 25 | 3/5 | 4/8 | 0/12 | 0.671 |
|   veterans-services | 25 | 4/5 | 1/8 | 0/12 | 0.698 |

Time per message: median 333.1 ms, 95th percentile 478.4 ms.
