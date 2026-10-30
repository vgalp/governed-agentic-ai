# Independent crisis benchmark v1: classifier, guard-modernbert-v6

Config `960ffcb2ed52`, run 2026-10-29T01:01:31, benchmark `0568fe69be2a`. Messages and labels from Aegis 2.0, SimpleSafetyTests and XSTest (see eval/datasets/crisis_benchmark_v1_protocol.md).

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 53/58 (91.4%, 95% CI 81.4–96.3) | 116/1135 (10.2%, 95% CI 8.6–12.1) | 202/1135 (17.8%, 95% CI 15.7–20.1) |
| clinic-assistant | 51/58 (87.9%, 95% CI 77.1–94.0) | 86/1135 (7.6%, 95% CI 6.2–9.3) | 167/1135 (14.7%, 95% CI 12.8–16.9) |
| county-benefits | 50/58 (86.2%, 95% CI 75.1–92.8) | 91/1135 (8.0%, 95% CI 6.6–9.7) | 187/1135 (16.5%, 95% CI 14.4–18.8) |
| education-staff (unseen) | 50/58 (86.2%, 95% CI 75.1–92.8) | 75/1135 (6.6%, 95% CI 5.3–8.2) | 132/1135 (11.6%, 95% CI 9.9–13.6) |
| housing-office (unseen) | 53/58 (91.4%, 95% CI 81.4–96.3) | 100/1135 (8.8%, 95% CI 7.3–10.6) | 161/1135 (14.2%, 95% CI 12.3–16.3) |
| legal-aid (unseen) | 49/58 (84.5%, 95% CI 73.1–91.6) | 83/1135 (7.3%, 95% CI 5.9–9.0) | 153/1135 (13.5%, 95% CI 11.6–15.6) |
| veterans-services (unseen) | 49/58 (84.5%, 95% CI 73.1–91.6) | 76/1135 (6.7%, 95% CI 5.4–8.3) | 151/1135 (13.3%, 95% CI 11.5–15.4) |
| **All policies** | 355/406 (87.4%, 95% CI 83.9–90.3) | 627/7945 (7.9%, 95% CI 7.3–8.5) | 1153/7945 (14.5%, 95% CI 13.8–15.3) |
| **Trained policies** | 154/174 (88.5%, 95% CI 82.9–92.4) | 293/3405 (8.6%, 95% CI 7.7–9.6) | 556/3405 (16.3%, 95% CI 15.1–17.6) |
| **Unseen policies** | 201/232 (86.6%, 95% CI 81.7–90.4) | 334/4540 (7.4%, 95% CI 6.6–8.2) | 597/4540 (13.2%, 95% CI 12.2–14.2) |
| **Messages of 100 words or fewer** | 243/294 (82.7%, 95% CI 77.9–86.6) | 544/7154 (7.6%, 95% CI 7.0–8.2) | 1054/7154 (14.7%, 95% CI 13.9–15.6) |
| **Messages over 100 words (split)** | 112/112 (100.0%, 95% CI 96.7–100.0) | 83/791 (10.5%, 95% CI 8.5–12.8) | 99/791 (12.5%, 95% CI 10.4–15.0) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| aegis2 | 230/266 (86.5%, 95% CI 81.8–90.1) | 602/6195 (9.7%, 95% CI 9.0–10.5) |
| sst | 125/140 (89.3%, 95% CI 83.1–93.4) | – |
| xstest | – | 25/1750 (1.4%, 95% CI 1.0–2.1) |

Crisis messages caught under every policy: 47/58 (81.0%, 95% CI 69.2–89.1); missed under every policy: 4/58 (6.9%, 95% CI 2.7–16.4). Not-crisis messages given the crisis reply under at least one policy: 135/1135 (11.9%, 95% CI 10.1–13.9).
