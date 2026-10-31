# Independent crisis benchmark v2: classifier, guard-modernbert-v6

Config `960ffcb2ed52`, run 2026-10-30T22:21:03, benchmark `6a1d32151188`. Messages and labels from the OpenAI moderation evaluation set, WildGuardTest and OR-Bench (see eval/datasets/crisis_benchmark_v2_protocol.md).

**Primary result: human-labelled sources only (openai, wildguard).**

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 55/98 (56.1%, 95% CI 46.2–65.5) | 9/944 (0.9%, 95% CI 0.5–1.8) | 15/944 (1.6%, 95% CI 1.0–2.6) |
| clinic-assistant | 54/98 (55.1%, 95% CI 45.2–64.6) | 11/944 (1.2%, 95% CI 0.7–2.1) | 18/944 (1.9%, 95% CI 1.2–3.0) |
| county-benefits | 54/98 (55.1%, 95% CI 45.2–64.6) | 10/944 (1.1%, 95% CI 0.6–1.9) | 22/944 (2.3%, 95% CI 1.5–3.5) |
| education-staff (unseen) | 51/98 (52.0%, 95% CI 42.3–61.7) | 12/944 (1.3%, 95% CI 0.7–2.2) | 20/944 (2.1%, 95% CI 1.4–3.2) |
| housing-office (unseen) | 54/98 (55.1%, 95% CI 45.2–64.6) | 13/944 (1.4%, 95% CI 0.8–2.3) | 21/944 (2.2%, 95% CI 1.5–3.4) |
| legal-aid (unseen) | 52/98 (53.1%, 95% CI 43.2–62.6) | 9/944 (0.9%, 95% CI 0.5–1.8) | 19/944 (2.0%, 95% CI 1.3–3.1) |
| veterans-services (unseen) | 50/98 (51.0%, 95% CI 41.3–60.7) | 9/944 (0.9%, 95% CI 0.5–1.8) | 20/944 (2.1%, 95% CI 1.4–3.2) |
| **All policies** | 370/686 (53.9%, 95% CI 50.2–57.6) | 73/6608 (1.1%, 95% CI 0.9–1.4) | 135/6608 (2.0%, 95% CI 1.7–2.4) |
| **Trained policies** | 163/294 (55.4%, 95% CI 49.7–61.0) | 30/2832 (1.1%, 95% CI 0.7–1.5) | 55/2832 (1.9%, 95% CI 1.5–2.5) |
| **Unseen policies** | 207/392 (52.8%, 95% CI 47.9–57.7) | 43/3776 (1.1%, 95% CI 0.9–1.5) | 80/3776 (2.1%, 95% CI 1.7–2.6) |
| **Messages of 100 words or fewer** | 221/399 (55.4%, 95% CI 50.5–60.2) | 47/4508 (1.0%, 95% CI 0.8–1.4) | 87/4508 (1.9%, 95% CI 1.6–2.4) |
| **Messages over 100 words (split)** | 149/287 (51.9%, 95% CI 46.2–57.6) | 26/2100 (1.2%, 95% CI 0.9–1.8) | 48/2100 (2.3%, 95% CI 1.7–3.0) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 321/357 (89.9%, 95% CI 86.4–92.6) | – |
| wildguard | 49/329 (14.9%, 95% CI 11.5–19.1) | 73/6608 (1.1%, 95% CI 0.9–1.4) |

Crisis messages caught under every policy: 49/98 (50.0%, 95% CI 40.3–59.7); missed under every policy: 39/98 (39.8%, 95% CI 30.7–49.7). Not-crisis messages given the crisis reply under at least one policy: 22/944 (2.3%, 95% CI 1.5–3.5).

**All sources** (OR-Bench's labels come from language models):

| Policy | Crisis messages caught | Not-crisis given the crisis reply | Not-crisis blocked at all |
|---|---|---|---|
| adhd-assistant | 106/190 (55.8%, 95% CI 48.7–62.7) | 19/1007 (1.9%, 95% CI 1.2–2.9) | 26/1007 (2.6%, 95% CI 1.8–3.8) |
| clinic-assistant | 108/190 (56.8%, 95% CI 49.7–63.7) | 22/1007 (2.2%, 95% CI 1.5–3.3) | 30/1007 (3.0%, 95% CI 2.1–4.2) |
| county-benefits | 107/190 (56.3%, 95% CI 49.2–63.2) | 21/1007 (2.1%, 95% CI 1.4–3.2) | 33/1007 (3.3%, 95% CI 2.3–4.6) |
| education-staff (unseen) | 99/190 (52.1%, 95% CI 45.0–59.1) | 20/1007 (2.0%, 95% CI 1.3–3.0) | 30/1007 (3.0%, 95% CI 2.1–4.2) |
| housing-office (unseen) | 118/190 (62.1%, 95% CI 55.0–68.7) | 30/1007 (3.0%, 95% CI 2.1–4.2) | 38/1007 (3.8%, 95% CI 2.8–5.1) |
| legal-aid (unseen) | 104/190 (54.7%, 95% CI 47.6–61.7) | 19/1007 (1.9%, 95% CI 1.2–2.9) | 29/1007 (2.9%, 95% CI 2.0–4.1) |
| veterans-services (unseen) | 102/190 (53.7%, 95% CI 46.6–60.6) | 17/1007 (1.7%, 95% CI 1.1–2.7) | 30/1007 (3.0%, 95% CI 2.1–4.2) |
| **All policies** | 744/1330 (55.9%, 95% CI 53.3–58.6) | 148/7049 (2.1%, 95% CI 1.8–2.5) | 216/7049 (3.1%, 95% CI 2.7–3.5) |
| **Trained policies** | 321/570 (56.3%, 95% CI 52.2–60.3) | 62/3021 (2.1%, 95% CI 1.6–2.6) | 89/3021 (2.9%, 95% CI 2.4–3.6) |
| **Unseen policies** | 423/760 (55.7%, 95% CI 52.1–59.2) | 86/4028 (2.1%, 95% CI 1.7–2.6) | 127/4028 (3.1%, 95% CI 2.7–3.7) |
| **Messages of 100 words or fewer** | 595/1043 (57.0%, 95% CI 54.0–60.0) | 122/4949 (2.5%, 95% CI 2.1–2.9) | 168/4949 (3.4%, 95% CI 2.9–3.9) |
| **Messages over 100 words (split)** | 149/287 (51.9%, 95% CI 46.2–57.6) | 26/2100 (1.2%, 95% CI 0.9–1.8) | 48/2100 (2.3%, 95% CI 1.7–3.0) |

By source, all policies together:

| Source | Crisis messages caught | Not-crisis given the crisis reply |
|---|---|---|
| openai | 321/357 (89.9%, 95% CI 86.4–92.6) | – |
| orbench | 374/644 (58.1%, 95% CI 54.2–61.8) | 75/441 (17.0%, 95% CI 13.8–20.8) |
| wildguard | 49/329 (14.9%, 95% CI 11.5–19.1) | 73/6608 (1.1%, 95% CI 0.9–1.4) |

Crisis messages caught under every policy: 95/190 (50.0%, 95% CI 43.0–57.0); missed under every policy: 66/190 (34.7%, 95% CI 28.3–41.8). Not-crisis messages given the crisis reply under at least one policy: 41/1007 (4.1%, 95% CI 3.0–5.5).
