# Sensitive categories: qwen2.5-7b on sensitivity_human_test_v1.jsonl

Dataset /Users/vladgalperin/Desktop/des/sensitivity_human_test_v1.jsonl, rerunnable. Value sets b4f10f83f0ef. 2026-11-16T13:28:52-05:00.

| Category | Explicit wording caught | Implied only caught | Coded resources caught | Marked on rows without it |
|---|---|---|---|---|
| Substance use and its treatment | 23/23 | 22/24 | – | 26/1462 |
| Mental health | 33/39 | 9/21 | – | 8/1449 |
| HIV status, testing and treatment | 28/30 | 4/7 | – | 14/1472 |
| Sexually transmitted infections | 15/19 | 4/6 | – | 22/1484 |
| Reproductive health | 34/38 | 8/19 | – | 11/1452 |
| Abuse, assault and domestic violence | 2/2 | 6/7 | – | 2/1500 |
| Genetic information | 14/14 | 22/44 | – | 3/1451 |

| Rows with no category | Marked anyway |
|---|---|
| Look sensitive but are not (hard negatives) | 57/674 |
| Everyday text | 13/563 |
| Resources | 0/0 |

Rows with exactly the right categories: 1365/1509.

Model qwen2.5:7b: 1509 calls, median 0.292 s each, 0 replies that could not be read.
