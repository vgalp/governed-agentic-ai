# Independent crisis benchmark v1: llamaguard

Config `1668457617d9`, run 2026-10-22T01:39:26, benchmark `0568fe69be2a`. Messages and labels from Aegis 2.0, SimpleSafetyTests and XSTest (see eval/datasets/crisis_benchmark_v1_protocol.md).

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| clinic-assistant | 51/58 (87.9%, 95% CI 77.1–94.0) | 40/1135 (3.5%, 95% CI 2.6–4.8) | 45/1135 (4.0%, 95% CI 3.0–5.3) |
| education-staff (unseen) | 51/58 (87.9%, 95% CI 77.1–94.0) | 47/1135 (4.1%, 95% CI 3.1–5.5) | 51/1135 (4.5%, 95% CI 3.4–5.9) |
| **All policies** | 102/116 (87.9%, 95% CI 80.8–92.7) | 87/2270 (3.8%, 95% CI 3.1–4.7) | 96/2270 (4.2%, 95% CI 3.5–5.1) |
| **Trained policies** | 51/58 (87.9%, 95% CI 77.1–94.0) | 40/1135 (3.5%, 95% CI 2.6–4.8) | 45/1135 (4.0%, 95% CI 3.0–5.3) |
| **Unseen policies** | 51/58 (87.9%, 95% CI 77.1–94.0) | 47/1135 (4.1%, 95% CI 3.1–5.5) | 51/1135 (4.5%, 95% CI 3.4–5.9) |
| **Messages of 100 words or fewer** | 70/84 (83.3%, 95% CI 74.0–89.8) | 75/2044 (3.7%, 95% CI 2.9–4.6) | 82/2044 (4.0%, 95% CI 3.2–5.0) |
| **Messages over 100 words (split)** | 32/32 (100.0%, 95% CI 89.3–100.0) | 12/226 (5.3%, 95% CI 3.1–9.0) | 14/226 (6.2%, 95% CI 3.7–10.1) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| aegis2 | 64/76 (84.2%, 95% CI 74.4–90.7) | 83/1770 (4.7%, 95% CI 3.8–5.8) |
| sst | 38/40 (95.0%, 95% CI 83.5–98.6) | – |
| xstest | – | 4/500 (0.8%, 95% CI 0.3–2.0) |

Crisis messages caught under every policy: 51/58 (87.9%, 95% CI 77.1–94.0); missed under every policy: 7/58 (12.1%, 95% CI 6.0–22.9). Not-crisis messages given the crisis reply under at least one policy: 52/1135 (4.6%, 95% CI 3.5–6.0).
