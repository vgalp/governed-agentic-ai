# Tool-use accuracy on tool_use_v1.jsonl

Routing configuration `f2284e448c44`, run 2026-10-25T22:41:14. Routing only: where a question that passed the input checks goes, and whether the role may.

| Set | Questions | Right destination | Right permission | Both right |
|---|---|---|---|---|
| **All** | 61 | 45/61 (73.8%, 95% CI 61.6–83.2) | 58/61 (95.1%, 95% CI 86.5–98.3) | 45/61 (73.8%, 95% CI 61.6–83.2) |
| kind: plain | 33 | 33/33 (100.0%, 95% CI 89.6–100.0) | 33/33 (100.0%, 95% CI 89.6–100.0) | 33/33 (100.0%, 95% CI 89.6–100.0) |
| kind: paraphrase | 10 | 0/10 (0.0%, 95% CI 0.0–27.8) | 10/10 (100.0%, 95% CI 72.2–100.0) | 0/10 (0.0%, 95% CI 0.0–27.8) |
| kind: near_miss | 10 | 4/10 (40.0%, 95% CI 16.8–68.7) | 7/10 (70.0%, 95% CI 39.7–89.2) | 4/10 (40.0%, 95% CI 16.8–68.7) |
| kind: role | 8 | 8/8 (100.0%, 95% CI 67.6–100.0) | 8/8 (100.0%, 95% CI 67.6–100.0) | 8/8 (100.0%, 95% CI 67.6–100.0) |
| profile: clinic-assistant | 33 | 24/33 (72.7%, 95% CI 55.8–84.9) | 32/33 (97.0%, 95% CI 84.7–99.5) | 24/33 (72.7%, 95% CI 55.8–84.9) |
| profile: clinic-epic | 11 | 8/11 (72.7%, 95% CI 43.4–90.2) | 10/11 (90.9%, 95% CI 62.3–98.4) | 8/11 (72.7%, 95% CI 43.4–90.2) |
| profile: county-benefits | 17 | 13/17 (76.5%, 95% CI 52.7–90.5) | 16/17 (94.1%, 95% CI 73.0–99.0) | 13/17 (76.5%, 95% CI 52.7–90.5) |

| Destination | Precision | Recall |
|---|---|---|
| change:release_payment | 6/8 (75.0%, 95% CI 40.9–92.8) | 6/8 (75.0%, 95% CI 40.9–92.8) |
| change:reschedule_appointment | 5/7 (71.4%, 95% CI 35.9–91.8) | 5/7 (71.4%, 95% CI 35.9–91.8) |
| general | 22/32 (68.8%, 95% CI 51.4–82.0) | 22/28 (78.6%, 95% CI 60.5–89.8) |
| tool:medication_list | 4/5 (80.0%, 95% CI 37.5–96.4) | 4/6 (66.7%, 95% CI 30.0–90.3) |
| tool:medication_review | 8/9 (88.9%, 95% CI 56.5–98.0) | 8/12 (66.7%, 95% CI 39.1–86.2) |

Missed (should have gone to a tool or a change, went to the general tools): 10. False triggers (a general question sent to a tool or a change): 6. Right destination, wrong permission: 0.

Wrong:

| ID | Kind | Role | Question | Expected | Got |
|---|---|---|---|---|---|
| tu1-ca-17 | paraphrase | clinician | Could Harold Brinkley's warfarin and his aspirin cause a problem together? | tool:medication_review (allowed) | general (allowed) |
| tu1-ca-18 | paraphrase | clinician | Is there any drug-drug conflict on Doris Whitfield's list? | tool:medication_review (allowed) | general (allowed) |
| tu1-ca-19 | paraphrase | clinician | Review Harold Brinkley's prescriptions for anything that shouldn't be taken together. | tool:medication_review (allowed) | general (allowed) |
| tu1-ca-20 | paraphrase | clinician | Are Lena Okafor's current medicines safe to combine according to the labels? | tool:medication_review (allowed) | general (allowed) |
| tu1-ca-26 | paraphrase | front_desk | Harold Brinkley needs a different appointment time, can you book him for Wednesday instead? | change:reschedule_appointment (allowed) | general (allowed) |
| tu1-ca-27 | paraphrase | nurse | Doris Whitfield can't make it on the 14th, please switch her to the 16th. | change:reschedule_appointment (allowed) | general (allowed) |
| tu1-ca-30 | near_miss | front_desk | Why did the visit get moved last week? | general (allowed) | change:reschedule_appointment (allowed) |
| tu1-ca-31 | near_miss | nurse | How long does a medication review appointment usually take? | general (allowed) | tool:medication_review (denied) |
| tu1-ca-33 | near_miss | front_desk | What is our policy when a patient asks to move an appointment online? | general (allowed) | change:reschedule_appointment (allowed) |
| tu1-ce-09 | paraphrase | nurse | Is Camila Lopez (1987-09-12) on anything right now? | tool:medication_list (allowed) | general (allowed) |
| tu1-ce-10 | paraphrase | clinician | What is she prescribed at the moment: Camila Lopez, born 1987-09-12? | tool:medication_list (allowed) | general (allowed) |
| tu1-ce-11 | near_miss | compliance | What does our policy say about medication reconciliation? | general (allowed) | tool:medication_list (denied) |
| tu1-cb-13 | paraphrase | eligibility_worker | Rosa Diaz's payment is stuck, can we get it issued? | change:release_payment (allowed) | general (allowed) |
| tu1-cb-14 | paraphrase | supervisor | Send out the held benefit check for Dana Whitfield. | change:release_payment (allowed) | general (allowed) |
| tu1-cb-16 | near_miss | eligibility_worker | Has the payment for Marcus Lee been released yet? | general (allowed) | change:release_payment (allowed) |
| tu1-cb-17 | near_miss | auditor | How many payments were released last month under our hold policy? | general (allowed) | change:release_payment (denied) |
