# Independent crisis benchmark v2: classifier, guard-modernbert-v6.1

Config `c58d0c88d050`, run 2026-10-30T22:13:58, benchmark `6a1d32151188`. Messages and labels from the OpenAI moderation evaluation set, WildGuardTest and OR-Bench (see eval/datasets/crisis_benchmark_v2_protocol.md).

**Primary result: human-labelled sources only (openai, wildguard).**

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 60/98 (61.2%, 95% CI 51.3–70.3) | 21/944 (2.2%, 95% CI 1.5–3.4) | 29/944 (3.1%, 95% CI 2.1–4.4) |
| clinic-assistant | 60/98 (61.2%, 95% CI 51.3–70.3) | 29/944 (3.1%, 95% CI 2.1–4.4) | 37/944 (3.9%, 95% CI 2.9–5.4) |
| county-benefits | 62/98 (63.3%, 95% CI 53.4–72.1) | 32/944 (3.4%, 95% CI 2.4–4.8) | 44/944 (4.7%, 95% CI 3.5–6.2) |
| education-staff (unseen) | 62/98 (63.3%, 95% CI 53.4–72.1) | 35/944 (3.7%, 95% CI 2.7–5.1) | 46/944 (4.9%, 95% CI 3.7–6.4) |
| housing-office (unseen) | 65/98 (66.3%, 95% CI 56.5–74.9) | 46/944 (4.9%, 95% CI 3.7–6.4) | 55/944 (5.8%, 95% CI 4.5–7.5) |
| legal-aid (unseen) | 60/98 (61.2%, 95% CI 51.3–70.3) | 31/944 (3.3%, 95% CI 2.3–4.6) | 41/944 (4.3%, 95% CI 3.2–5.8) |
| veterans-services (unseen) | 61/98 (62.2%, 95% CI 52.4–71.2) | 31/944 (3.3%, 95% CI 2.3–4.6) | 42/944 (4.5%, 95% CI 3.3–6.0) |
| **All policies** | 430/686 (62.7%, 95% CI 59.0–66.2) | 225/6608 (3.4%, 95% CI 3.0–3.9) | 294/6608 (4.5%, 95% CI 4.0–5.0) |
| **Trained policies** | 182/294 (61.9%, 95% CI 56.2–67.3) | 82/2832 (2.9%, 95% CI 2.3–3.6) | 110/2832 (3.9%, 95% CI 3.2–4.7) |
| **Unseen policies** | 248/392 (63.3%, 95% CI 58.4–67.9) | 143/3776 (3.8%, 95% CI 3.2–4.4) | 184/3776 (4.9%, 95% CI 4.2–5.6) |
| **Messages of 100 words or fewer** | 258/399 (64.7%, 95% CI 59.9–69.2) | 137/4508 (3.0%, 95% CI 2.6–3.6) | 185/4508 (4.1%, 95% CI 3.6–4.7) |
| **Messages over 100 words (split)** | 172/287 (59.9%, 95% CI 54.2–65.4) | 88/2100 (4.2%, 95% CI 3.4–5.1) | 109/2100 (5.2%, 95% CI 4.3–6.2) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 350/357 (98.0%, 95% CI 96.0–99.1) | – |
| wildguard | 80/329 (24.3%, 95% CI 20.0–29.2) | 225/6608 (3.4%, 95% CI 3.0–3.9) |

Crisis messages caught under every policy: 59/98 (60.2%, 95% CI 50.3–69.3); missed under every policy: 30/98 (30.6%, 95% CI 22.4–40.3). Not-crisis messages given the crisis reply under at least one policy: 62/944 (6.6%, 95% CI 5.2–8.3).

**All sources** (OR-Bench's labels come from language models):

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 126/190 (66.3%, 95% CI 59.3–72.7) | 35/1007 (3.5%, 95% CI 2.5–4.8) | 45/1007 (4.5%, 95% CI 3.4–5.9) |
| clinic-assistant | 131/190 (69.0%, 95% CI 62.1–75.1) | 46/1007 (4.6%, 95% CI 3.4–6.0) | 56/1007 (5.6%, 95% CI 4.3–7.1) |
| county-benefits | 140/190 (73.7%, 95% CI 67.0–79.4) | 53/1007 (5.3%, 95% CI 4.0–6.8) | 67/1007 (6.7%, 95% CI 5.3–8.4) |
| education-staff (unseen) | 132/190 (69.5%, 95% CI 62.6–75.6) | 50/1007 (5.0%, 95% CI 3.8–6.5) | 63/1007 (6.3%, 95% CI 4.9–7.9) |
| housing-office (unseen) | 149/190 (78.4%, 95% CI 72.0–83.7) | 76/1007 (7.5%, 95% CI 6.1–9.3) | 88/1007 (8.7%, 95% CI 7.1–10.6) |
| legal-aid (unseen) | 132/190 (69.5%, 95% CI 62.6–75.6) | 49/1007 (4.9%, 95% CI 3.7–6.4) | 61/1007 (6.1%, 95% CI 4.7–7.7) |
| veterans-services (unseen) | 134/190 (70.5%, 95% CI 63.7–76.5) | 48/1007 (4.8%, 95% CI 3.6–6.3) | 62/1007 (6.2%, 95% CI 4.8–7.8) |
| **All policies** | 944/1330 (71.0%, 95% CI 68.5–73.4) | 357/7049 (5.1%, 95% CI 4.6–5.6) | 442/7049 (6.3%, 95% CI 5.7–6.9) |
| **Trained policies** | 397/570 (69.7%, 95% CI 65.8–73.3) | 134/3021 (4.4%, 95% CI 3.8–5.2) | 168/3021 (5.6%, 95% CI 4.8–6.4) |
| **Unseen policies** | 547/760 (72.0%, 95% CI 68.7–75.0) | 223/4028 (5.5%, 95% CI 4.9–6.3) | 274/4028 (6.8%, 95% CI 6.1–7.6) |
| **Messages of 100 words or fewer** | 772/1043 (74.0%, 95% CI 71.3–76.6) | 269/4949 (5.4%, 95% CI 4.8–6.1) | 333/4949 (6.7%, 95% CI 6.1–7.5) |
| **Messages over 100 words (split)** | 172/287 (59.9%, 95% CI 54.2–65.4) | 88/2100 (4.2%, 95% CI 3.4–5.1) | 109/2100 (5.2%, 95% CI 4.3–6.2) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 350/357 (98.0%, 95% CI 96.0–99.1) | – |
| orbench | 514/644 (79.8%, 95% CI 76.5–82.7) | 132/441 (29.9%, 95% CI 25.9–34.4) |
| wildguard | 80/329 (24.3%, 95% CI 20.0–29.2) | 225/6608 (3.4%, 95% CI 3.0–3.9) |

Crisis messages caught under every policy: 121/190 (63.7%, 95% CI 56.6–70.2); missed under every policy: 38/190 (20.0%, 95% CI 14.9–26.3). Not-crisis messages given the crisis reply under at least one policy: 92/1007 (9.1%, 95% CI 7.5–11.1).
