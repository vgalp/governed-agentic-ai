# Baseline: rules+classifier on frozen test set v4

Config `f85591407ded`, run 2026-10-12T22:06:11.

| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |
|---|---|---|---|---|---|---|
| All | 150 | 4/30 | 6/48 | 1/72 | 0.925 | 0/72 |
| Seen profiles | 75 | 4/15 | 0/24 | 0/36 | 0.929 | 0/36 |
| Unseen profiles (housing-office, legal-aid, veterans-services) | 75 | 0/15 | 6/24 | 1/36 | 0.915 | 0/36 |
|   housing-office | 25 | 0/5 | 3/8 | 1/12 | 0.854 | 0/12 |
|   legal-aid | 25 | 0/5 | 1/8 | 0/12 | 0.964 | 0/12 |
|   veterans-services | 25 | 0/5 | 2/8 | 0/12 | 0.927 | 0/12 |

Time per message: median 14.9 ms, 95th percentile 17.9 ms.
