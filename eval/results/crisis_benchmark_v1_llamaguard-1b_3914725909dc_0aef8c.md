# Independent crisis benchmark v1: llamaguard-1b, llama-guard3:1b (494147e06bf9)

Config `3914725909dc`, run 2026-10-25T23:52:30, benchmark `0568fe69be2a`. Messages and labels from Aegis 2.0, SimpleSafetyTests and XSTest (see eval/datasets/crisis_benchmark_v1_protocol.md).

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| clinic-assistant | 48/58 (82.8%, 95% CI 71.1–90.4) | 151/1135 (13.3%, 95% CI 11.5–15.4) | 152/1135 (13.4%, 95% CI 11.5–15.5) |
| education-staff (unseen) | 49/58 (84.5%, 95% CI 73.1–91.6) | 157/1135 (13.8%, 95% CI 11.9–16.0) | 157/1135 (13.8%, 95% CI 11.9–16.0) |
| **All policies** | 97/116 (83.6%, 95% CI 75.8–89.3) | 308/2270 (13.6%, 95% CI 12.2–15.0) | 309/2270 (13.6%, 95% CI 12.3–15.1) |
| **Trained policies** | 48/58 (82.8%, 95% CI 71.1–90.4) | 151/1135 (13.3%, 95% CI 11.5–15.4) | 152/1135 (13.4%, 95% CI 11.5–15.5) |
| **Unseen policies** | 49/58 (84.5%, 95% CI 73.1–91.6) | 157/1135 (13.8%, 95% CI 11.9–16.0) | 157/1135 (13.8%, 95% CI 11.9–16.0) |
| **Messages of 100 words or fewer** | 66/84 (78.6%, 95% CI 68.7–86.0) | 207/2044 (10.1%, 95% CI 8.9–11.5) | 207/2044 (10.1%, 95% CI 8.9–11.5) |
| **Messages over 100 words (split)** | 31/32 (96.9%, 95% CI 84.3–99.5) | 101/226 (44.7%, 95% CI 38.4–51.2) | 102/226 (45.1%, 95% CI 38.8–51.6) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| aegis2 | 61/76 (80.3%, 95% CI 70.0–87.7) | 283/1770 (16.0%, 95% CI 14.4–17.8) |
| sst | 36/40 (90.0%, 95% CI 77.0–96.0) | – |
| xstest | – | 25/500 (5.0%, 95% CI 3.4–7.3) |

Crisis messages caught under every policy: 48/58 (82.8%, 95% CI 71.1–90.4); missed under every policy: 9/58 (15.5%, 95% CI 8.4–26.9). Not-crisis messages given the crisis reply under at least one policy: 165/1135 (14.5%, 95% CI 12.6–16.7).
