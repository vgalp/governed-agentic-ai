# Independent crisis benchmark v2: classifier, guard-loop-trial1-r001

Config `f4e079b7fdd2`, run 2026-11-14T00:07:01, benchmark `6a1d32151188`. Messages and labels from the OpenAI moderation evaluation set, WildGuardTest and OR-Bench (see eval/datasets/crisis_benchmark_v2_protocol.md).

**Primary result: human-labelled sources only (openai, wildguard).**

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 69/98 (70.4%, 95% CI 60.7–78.5) | 60/944 (6.4%, 95% CI 5.0–8.1) | 70/944 (7.4%, 95% CI 5.9–9.3) |
| clinic-assistant | 72/98 (73.5%, 95% CI 64.0–81.2) | 73/944 (7.7%, 95% CI 6.2–9.6) | 83/944 (8.8%, 95% CI 7.1–10.8) |
| county-benefits | 72/98 (73.5%, 95% CI 64.0–81.2) | 80/944 (8.5%, 95% CI 6.9–10.4) | 91/944 (9.6%, 95% CI 7.9–11.7) |
| education-staff (unseen) | 76/98 (77.5%, 95% CI 68.3–84.7) | 91/944 (9.6%, 95% CI 7.9–11.7) | 100/944 (10.6%, 95% CI 8.8–12.7) |
| housing-office (unseen) | 77/98 (78.6%, 95% CI 69.5–85.5) | 111/944 (11.8%, 95% CI 9.9–14.0) | 121/944 (12.8%, 95% CI 10.8–15.1) |
| legal-aid (unseen) | 74/98 (75.5%, 95% CI 66.1–83.0) | 89/944 (9.4%, 95% CI 7.7–11.5) | 98/944 (10.4%, 95% CI 8.6–12.5) |
| veterans-services (unseen) | 74/98 (75.5%, 95% CI 66.1–83.0) | 102/944 (10.8%, 95% CI 9.0–13.0) | 114/944 (12.1%, 95% CI 10.2–14.3) |
| **All policies** | 514/686 (74.9%, 95% CI 71.5–78.0) | 606/6608 (9.2%, 95% CI 8.5–9.9) | 677/6608 (10.2%, 95% CI 9.5–11.0) |
| **Trained policies** | 213/294 (72.5%, 95% CI 67.1–77.2) | 213/2832 (7.5%, 95% CI 6.6–8.6) | 244/2832 (8.6%, 95% CI 7.6–9.7) |
| **Unseen policies** | 301/392 (76.8%, 95% CI 72.4–80.7) | 393/3776 (10.4%, 95% CI 9.5–11.4) | 433/3776 (11.5%, 95% CI 10.5–12.5) |
| **Messages of 100 words or fewer** | 297/399 (74.4%, 95% CI 69.9–78.5) | 326/4508 (7.2%, 95% CI 6.5–8.0) | 382/4508 (8.5%, 95% CI 7.7–9.3) |
| **Messages over 100 words (split)** | 217/287 (75.6%, 95% CI 70.3–80.2) | 280/2100 (13.3%, 95% CI 11.9–14.8) | 295/2100 (14.1%, 95% CI 12.6–15.6) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 350/357 (98.0%, 95% CI 96.0–99.1) | – |
| wildguard | 164/329 (49.9%, 95% CI 44.5–55.2) | 606/6608 (9.2%, 95% CI 8.5–9.9) |

Crisis messages caught under every policy: 67/98 (68.4%, 95% CI 58.6–76.7); missed under every policy: 16/98 (16.3%, 95% CI 10.3–24.9). Not-crisis messages given the crisis reply under at least one policy: 140/944 (14.8%, 95% CI 12.7–17.2).

**All sources** (OR-Bench's labels come from language models):

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 155/190 (81.6%, 95% CI 75.5–86.5) | 99/1007 (9.8%, 95% CI 8.1–11.8) | 109/1007 (10.8%, 95% CI 9.0–12.9) |
| clinic-assistant | 163/190 (85.8%, 95% CI 80.1–90.0) | 115/1007 (11.4%, 95% CI 9.6–13.5) | 126/1007 (12.5%, 95% CI 10.6–14.7) |
| county-benefits | 163/190 (85.8%, 95% CI 80.1–90.0) | 122/1007 (12.1%, 95% CI 10.2–14.3) | 133/1007 (13.2%, 95% CI 11.3–15.4) |
| education-staff (unseen) | 166/190 (87.4%, 95% CI 81.9–91.4) | 132/1007 (13.1%, 95% CI 11.2–15.3) | 142/1007 (14.1%, 95% CI 12.1–16.4) |
| housing-office (unseen) | 169/190 (88.9%, 95% CI 83.7–92.7) | 156/1007 (15.5%, 95% CI 13.4–17.9) | 167/1007 (16.6%, 95% CI 14.4–19.0) |
| legal-aid (unseen) | 165/190 (86.8%, 95% CI 81.3–90.9) | 131/1007 (13.0%, 95% CI 11.1–15.2) | 140/1007 (13.9%, 95% CI 11.9–16.2) |
| veterans-services (unseen) | 165/190 (86.8%, 95% CI 81.3–90.9) | 144/1007 (14.3%, 95% CI 12.3–16.6) | 157/1007 (15.6%, 95% CI 13.5–18.0) |
| **All policies** | 1146/1330 (86.2%, 95% CI 84.2–87.9) | 899/7049 (12.8%, 95% CI 12.0–13.6) | 974/7049 (13.8%, 95% CI 13.0–14.6) |
| **Trained policies** | 481/570 (84.4%, 95% CI 81.2–87.1) | 336/3021 (11.1%, 95% CI 10.1–12.3) | 368/3021 (12.2%, 95% CI 11.1–13.4) |
| **Unseen policies** | 665/760 (87.5%, 95% CI 85.0–89.7) | 563/4028 (14.0%, 95% CI 12.9–15.1) | 606/4028 (15.0%, 95% CI 14.0–16.2) |
| **Messages of 100 words or fewer** | 929/1043 (89.1%, 95% CI 87.0–90.8) | 619/4949 (12.5%, 95% CI 11.6–13.5) | 679/4949 (13.7%, 95% CI 12.8–14.7) |
| **Messages over 100 words (split)** | 217/287 (75.6%, 95% CI 70.3–80.2) | 280/2100 (13.3%, 95% CI 11.9–14.8) | 295/2100 (14.1%, 95% CI 12.6–15.6) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 350/357 (98.0%, 95% CI 96.0–99.1) | – |
| orbench | 632/644 (98.1%, 95% CI 96.8–98.9) | 293/441 (66.4%, 95% CI 61.9–70.7) |
| wildguard | 164/329 (49.9%, 95% CI 44.5–55.2) | 606/6608 (9.2%, 95% CI 8.5–9.9) |

Crisis messages caught under every policy: 152/190 (80.0%, 95% CI 73.7–85.1); missed under every policy: 16/190 (8.4%, 95% CI 5.2–13.2). Not-crisis messages given the crisis reply under at least one policy: 185/1007 (18.4%, 95% CI 16.1–20.9).
