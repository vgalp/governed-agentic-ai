# Baseline: llamaguard on frozen test set v2

Config `b721b1cce2d0`, run 2026-10-11T22:10:24.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |
|---|---|---|---|---|---|
| All | 150 | 24/30 | 13/45 | 1/75 | 0.650 |
| Seen profiles | 90 | 14/18 | 6/27 | 1/45 | 0.676 |
| Unseen profiles (housing-office, legal-aid) | 60 | 10/12 | 7/18 | 0/30 | 0.608 |
|   housing-office | 30 | 5/6 | 4/9 | 0/15 | 0.590 |
|   legal-aid | 30 | 5/6 | 3/9 | 0/15 | 0.625 |

Time per message: median 326.1 ms, 95th percentile 437.7 ms.
