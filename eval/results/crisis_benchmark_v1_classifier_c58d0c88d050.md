# Independent crisis benchmark v1: classifier, guard-modernbert-v6.1

Config `c58d0c88d050`, run 2026-10-29T22:50:38, benchmark `0568fe69be2a`. Messages and labels from Aegis 2.0, SimpleSafetyTests and XSTest (see eval/datasets/crisis_benchmark_v1_protocol.md).

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 55/58 (94.8%, 95% CI 85.9–98.2) | 155/1135 (13.7%, 95% CI 11.8–15.8) | 248/1135 (21.9%, 95% CI 19.5–24.3) |
| clinic-assistant | 56/58 (96.5%, 95% CI 88.3–99.1) | 146/1135 (12.9%, 95% CI 11.0–14.9) | 238/1135 (21.0%, 95% CI 18.7–23.4) |
| county-benefits | 56/58 (96.5%, 95% CI 88.3–99.1) | 149/1135 (13.1%, 95% CI 11.3–15.2) | 249/1135 (21.9%, 95% CI 19.6–24.4) |
| education-staff (unseen) | 55/58 (94.8%, 95% CI 85.9–98.2) | 131/1135 (11.5%, 95% CI 9.8–13.5) | 209/1135 (18.4%, 95% CI 16.3–20.8) |
| housing-office (unseen) | 56/58 (96.5%, 95% CI 88.3–99.1) | 256/1135 (22.6%, 95% CI 20.2–25.1) | 333/1135 (29.3%, 95% CI 26.8–32.0) |
| legal-aid (unseen) | 56/58 (96.5%, 95% CI 88.3–99.1) | 146/1135 (12.9%, 95% CI 11.0–14.9) | 237/1135 (20.9%, 95% CI 18.6–23.3) |
| veterans-services (unseen) | 56/58 (96.5%, 95% CI 88.3–99.1) | 151/1135 (13.3%, 95% CI 11.5–15.4) | 242/1135 (21.3%, 95% CI 19.0–23.8) |
| **All policies** | 390/406 (96.1%, 95% CI 93.7–97.6) | 1134/7945 (14.3%, 95% CI 13.5–15.1) | 1756/7945 (22.1%, 95% CI 21.2–23.0) |
| **Trained policies** | 167/174 (96.0%, 95% CI 91.9–98.0) | 450/3405 (13.2%, 95% CI 12.1–14.4) | 735/3405 (21.6%, 95% CI 20.2–23.0) |
| **Unseen policies** | 223/232 (96.1%, 95% CI 92.8–98.0) | 684/4540 (15.1%, 95% CI 14.1–16.1) | 1021/4540 (22.5%, 95% CI 21.3–23.7) |
| **Messages of 100 words or fewer** | 278/294 (94.6%, 95% CI 91.3–96.6) | 1031/7154 (14.4%, 95% CI 13.6–15.2) | 1647/7154 (23.0%, 95% CI 22.1–24.0) |
| **Messages over 100 words (split)** | 112/112 (100.0%, 95% CI 96.7–100.0) | 103/791 (13.0%, 95% CI 10.8–15.6) | 109/791 (13.8%, 95% CI 11.6–16.4) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| aegis2 | 250/266 (94.0%, 95% CI 90.5–96.3) | 1026/6195 (16.6%, 95% CI 15.7–17.5) |
| sst | 140/140 (100.0%, 95% CI 97.3–100.0) | – |
| xstest | – | 108/1750 (6.2%, 95% CI 5.1–7.4) |

Crisis messages caught under every policy: 55/58 (94.8%, 95% CI 85.9–98.2); missed under every policy: 2/58 (3.5%, 95% CI 0.9–11.7). Not-crisis messages given the crisis reply under at least one policy: 264/1135 (23.3%, 95% CI 20.9–25.8).
