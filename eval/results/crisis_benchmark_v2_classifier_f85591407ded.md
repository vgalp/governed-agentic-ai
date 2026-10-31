# Independent crisis benchmark v2: classifier, guard-modernbert-v4

Config `f85591407ded`, run 2026-10-30T22:32:28, benchmark `6a1d32151188`. Messages and labels from the OpenAI moderation evaluation set, WildGuardTest and OR-Bench (see eval/datasets/crisis_benchmark_v2_protocol.md).

**Primary result: human-labelled sources only (openai, wildguard).**

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 84/98 (85.7%, 95% CI 77.4–91.3) | 309/944 (32.7%, 95% CI 29.8–35.8) | 315/944 (33.4%, 95% CI 30.4–36.4) |
| clinic-assistant | 82/98 (83.7%, 95% CI 75.1–89.7) | 290/944 (30.7%, 95% CI 27.9–33.7) | 298/944 (31.6%, 95% CI 28.7–34.6) |
| county-benefits | 83/98 (84.7%, 95% CI 76.3–90.5) | 281/944 (29.8%, 95% CI 26.9–32.8) | 302/944 (32.0%, 95% CI 29.1–35.0) |
| education-staff (unseen) | 84/98 (85.7%, 95% CI 77.4–91.3) | 297/944 (31.5%, 95% CI 28.6–34.5) | 306/944 (32.4%, 95% CI 29.5–35.5) |
| housing-office (unseen) | 88/98 (89.8%, 95% CI 82.2–94.4) | 327/944 (34.6%, 95% CI 31.7–37.7) | 336/944 (35.6%, 95% CI 32.6–38.7) |
| legal-aid (unseen) | 87/98 (88.8%, 95% CI 81.0–93.6) | 307/944 (32.5%, 95% CI 29.6–35.6) | 321/944 (34.0%, 95% CI 31.1–37.1) |
| veterans-services (unseen) | 87/98 (88.8%, 95% CI 81.0–93.6) | 323/944 (34.2%, 95% CI 31.3–37.3) | 334/944 (35.4%, 95% CI 32.4–38.5) |
| **All policies** | 595/686 (86.7%, 95% CI 84.0–89.1) | 2134/6608 (32.3%, 95% CI 31.2–33.4) | 2212/6608 (33.5%, 95% CI 32.4–34.6) |
| **Trained policies** | 249/294 (84.7%, 95% CI 80.1–88.4) | 880/2832 (31.1%, 95% CI 29.4–32.8) | 915/2832 (32.3%, 95% CI 30.6–34.1) |
| **Unseen policies** | 346/392 (88.3%, 95% CI 84.7–91.1) | 1254/3776 (33.2%, 95% CI 31.7–34.7) | 1297/3776 (34.4%, 95% CI 32.9–35.9) |
| **Messages of 100 words or fewer** | 340/399 (85.2%, 95% CI 81.4–88.4) | 1040/4508 (23.1%, 95% CI 21.9–24.3) | 1104/4508 (24.5%, 95% CI 23.3–25.8) |
| **Messages over 100 words (split)** | 255/287 (88.8%, 95% CI 84.7–92.0) | 1094/2100 (52.1%, 95% CI 50.0–54.2) | 1108/2100 (52.8%, 95% CI 50.6–54.9) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 357/357 (100.0%, 95% CI 98.9–100.0) | – |
| wildguard | 238/329 (72.3%, 95% CI 67.3–76.9) | 2134/6608 (32.3%, 95% CI 31.2–33.4) |

Crisis messages caught under every policy: 80/98 (81.6%, 95% CI 72.8–88.0); missed under every policy: 8/98 (8.2%, 95% CI 4.2–15.3). Not-crisis messages given the crisis reply under at least one policy: 377/944 (39.9%, 95% CI 36.9–43.1).

**All sources** (OR-Bench's labels come from language models):

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 173/190 (91.0%, 95% CI 86.1–94.3) | 361/1007 (35.9%, 95% CI 33.0–38.9) | 367/1007 (36.4%, 95% CI 33.5–39.5) |
| clinic-assistant | 165/190 (86.8%, 95% CI 81.3–90.9) | 337/1007 (33.5%, 95% CI 30.6–36.4) | 346/1007 (34.4%, 95% CI 31.5–37.4) |
| county-benefits | 171/190 (90.0%, 95% CI 84.9–93.5) | 329/1007 (32.7%, 95% CI 29.8–35.6) | 351/1007 (34.9%, 95% CI 32.0–37.9) |
| education-staff (unseen) | 172/190 (90.5%, 95% CI 85.5–93.9) | 347/1007 (34.5%, 95% CI 31.6–37.5) | 357/1007 (35.4%, 95% CI 32.6–38.5) |
| housing-office (unseen) | 177/190 (93.2%, 95% CI 88.6–96.0) | 379/1007 (37.6%, 95% CI 34.7–40.7) | 389/1007 (38.6%, 95% CI 35.7–41.7) |
| legal-aid (unseen) | 175/190 (92.1%, 95% CI 87.4–95.2) | 359/1007 (35.6%, 95% CI 32.8–38.7) | 374/1007 (37.1%, 95% CI 34.2–40.2) |
| veterans-services (unseen) | 176/190 (92.6%, 95% CI 88.0–95.6) | 373/1007 (37.0%, 95% CI 34.1–40.1) | 385/1007 (38.2%, 95% CI 35.3–41.3) |
| **All policies** | 1209/1330 (90.9%, 95% CI 89.2–92.3) | 2485/7049 (35.2%, 95% CI 34.2–36.4) | 2569/7049 (36.4%, 95% CI 35.3–37.6) |
| **Trained policies** | 509/570 (89.3%, 95% CI 86.5–91.6) | 1027/3021 (34.0%, 95% CI 32.3–35.7) | 1064/3021 (35.2%, 95% CI 33.5–36.9) |
| **Unseen policies** | 700/760 (92.1%, 95% CI 90.0–93.8) | 1458/4028 (36.2%, 95% CI 34.7–37.7) | 1505/4028 (37.4%, 95% CI 35.9–38.9) |
| **Messages of 100 words or fewer** | 954/1043 (91.5%, 95% CI 89.6–93.0) | 1391/4949 (28.1%, 95% CI 26.9–29.4) | 1461/4949 (29.5%, 95% CI 28.3–30.8) |
| **Messages over 100 words (split)** | 255/287 (88.8%, 95% CI 84.7–92.0) | 1094/2100 (52.1%, 95% CI 50.0–54.2) | 1108/2100 (52.8%, 95% CI 50.6–54.9) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 357/357 (100.0%, 95% CI 98.9–100.0) | – |
| orbench | 614/644 (95.3%, 95% CI 93.4–96.7) | 351/441 (79.6%, 95% CI 75.6–83.1) |
| wildguard | 238/329 (72.3%, 95% CI 67.3–76.9) | 2134/6608 (32.3%, 95% CI 31.2–33.4) |

Crisis messages caught under every policy: 163/190 (85.8%, 95% CI 80.1–90.0); missed under every policy: 11/190 (5.8%, 95% CI 3.3–10.1). Not-crisis messages given the crisis reply under at least one policy: 430/1007 (42.7%, 95% CI 39.7–45.8).
