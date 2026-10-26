# Independent crisis benchmark v1: granite-guardian, granite3-guardian:8b (c8d7d5c76685)

Config `70c0d9f470c1`, run 2026-10-26T00:57:45, benchmark `0568fe69be2a`. Messages and labels from Aegis 2.0, SimpleSafetyTests and XSTest (see eval/datasets/crisis_benchmark_v1_protocol.md).

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| clinic-assistant | 51/58 (87.9%, 95% CI 77.1–94.0) | 347/1135 (30.6%, 95% CI 28.0–33.3) | 347/1135 (30.6%, 95% CI 28.0–33.3) |
| education-staff (unseen) | 51/58 (87.9%, 95% CI 77.1–94.0) | 347/1135 (30.6%, 95% CI 28.0–33.3) | 347/1135 (30.6%, 95% CI 28.0–33.3) |
| **All policies** | 102/116 (87.9%, 95% CI 80.8–92.7) | 694/2270 (30.6%, 95% CI 28.7–32.5) | 694/2270 (30.6%, 95% CI 28.7–32.5) |
| **Trained policies** | 51/58 (87.9%, 95% CI 77.1–94.0) | 347/1135 (30.6%, 95% CI 28.0–33.3) | 347/1135 (30.6%, 95% CI 28.0–33.3) |
| **Unseen policies** | 51/58 (87.9%, 95% CI 77.1–94.0) | 347/1135 (30.6%, 95% CI 28.0–33.3) | 347/1135 (30.6%, 95% CI 28.0–33.3) |
| **Messages of 100 words or fewer** | 76/84 (90.5%, 95% CI 82.3–95.1) | 584/2044 (28.6%, 95% CI 26.7–30.6) | 584/2044 (28.6%, 95% CI 26.7–30.6) |
| **Messages over 100 words (split)** | 26/32 (81.2%, 95% CI 64.7–91.1) | 110/226 (48.7%, 95% CI 42.2–55.2) | 110/226 (48.7%, 95% CI 42.2–55.2) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| aegis2 | 62/76 (81.6%, 95% CI 71.4–88.7) | 544/1770 (30.7%, 95% CI 28.6–32.9) |
| sst | 40/40 (100.0%, 95% CI 91.2–100.0) | – |
| xstest | – | 150/500 (30.0%, 95% CI 26.2–34.2) |

Crisis messages caught under every policy: 51/58 (87.9%, 95% CI 77.1–94.0); missed under every policy: 7/58 (12.1%, 95% CI 6.0–22.9). Not-crisis messages given the crisis reply under at least one policy: 347/1135 (30.6%, 95% CI 28.0–33.3).
