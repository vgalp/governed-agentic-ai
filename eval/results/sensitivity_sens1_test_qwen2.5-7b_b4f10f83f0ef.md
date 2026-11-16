# Sensitive categories: qwen2.5-7b on test.jsonl

Dataset modelops/runs/sens1/sensitivity/test.jsonl, rerunnable. Value sets b4f10f83f0ef. 2026-11-16T09:10:45-05:00.

| Category | Explicit wording caught | Implied only caught | Coded resources caught | Marked on rows without it |
|---|---|---|---|---|
| Substance use and its treatment | 31/31 | 37/37 | – | 11/657 |
| Mental health | 28/29 | 17/28 | – | 7/668 |
| HIV status, testing and treatment | 30/30 | – | – | 1/695 |
| Sexually transmitted infections | 23/23 | 13/19 | – | 8/683 |
| Reproductive health | 29/30 | 9/10 | – | 4/685 |
| Abuse, assault and domestic violence | 24/26 | 51/55 | – | 5/644 |
| Genetic information | 26/26 | 18/20 | – | 7/679 |

| Rows with no category | Marked anyway |
|---|---|
| Look sensitive but are not (hard negatives) | 31/270 |
| Everyday text | 0/100 |
| Resources | 0/0 |

Rows with exactly the right categories: 660/725.

Model qwen2.5:7b: 725 calls, median 0.37 s each, 0 replies that could not be read.
