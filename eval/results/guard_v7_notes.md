# Guard classifier v7: notes, and why the profiles use it

Runs on 2026-11-14. `guard-modernbert-v7` is round 1 of the training loop's run `trial1`
(`guard-loop-trial1-r001`, weights `f4e079b7fdd2`, ADR 037), renamed for release. It was scored once on
crisis benchmark v2 and once on frozen test set v5. The result files in this folder are the official
results; this note explains them and records the choice.

| Model | Weights | Thresholds (chosen on dev) | Role |
|---|---|---|---|
| guard-modernbert-v6.1 | `c58d0c88d050` | crisis 0.65, decision 0.25 | the profiles' guard until this release |
| guard-modernbert-v7 | `f4e079b7fdd2` | in the model's `thresholds.json` (calibrated to at most 5% false alarms on dev; v6.1 used 2%) | **now the profiles' guard** |

## The result in one sentence
On the independent, human-labelled benchmark, v7 catches 74.9% of crisis messages against 62.7% for
v6.1, and gives the crisis reply to 9.2% of the other messages against 3.4%. On frozen test set v5 the
two are level. Quote the two benchmark numbers together, never the first alone.

## Crisis benchmark v2 (primary result: human-labelled sources, all seven policies)

| Measure | v6.1 | v7 |
|---|---|---|
| Crisis messages caught | 430/686 (62.7%, 95% CI 59.0–66.2) | 514/686 (74.9%, 95% CI 71.5–78.0) |
| Not-crisis given the crisis reply | 225/6608 (3.4%) | 606/6608 (9.2%, 95% CI 8.5–9.9) |
| Not-crisis blocked at all | 294/6608 (4.5%) | 677/6608 (10.2%) |
| WildGuardTest crisis messages caught | 80/329 (24.3%) | 164/329 (49.9%) |
| OpenAI moderation set crisis messages caught | 350/357 (98.0%) | 350/357 (98.0%) |
| Crisis messages missed under every policy | 30/98 | 16/98 |
| Policies never trained on: crisis caught | 248/392 (63.3%) | 301/392 (76.8%) |

- **The gain is on the hard messages.** The OpenAI set was already at 98%; WildGuardTest doubled.
- **False alarms nearly tripled.** They are highest on messages over 100 words (13.3%), and on
  OR-Bench's safe-but-alarming prompts (66.4% against 29.9%; OR-Bench labels come from language models
  and are not part of the primary result).
- **Part of the gain is a looser threshold, not a better model.** v7 was calibrated to allow 5% false
  alarms on dev where v6.1 allowed 2%. v4, v6.1 and v7 sit along one trade-off: v4 caught 86.7% with
  32.3% false alarms. This run cannot say how much of v7's gain is the threshold. Re-tuning on the
  benchmark to find out would end its use as a test, and was not done.
- **Half of WildGuardTest's crisis messages are still missed.**

## Frozen test set v5 (175 messages)

| Measure | v6.1 | v7 |
|---|---|---|
| Crisis missed | 2/35 | 1/35 |
| Decisions let through | 6/56 | 5/56 |
| Wrong blocks | 1/84 | 3/84 |
| Everyday messages given the crisis reply | 0/84 | 2/84 |
| Macro F1 | 0.951 | 0.942 |

Each difference is one or two messages. v7 missed no crisis message in the four unseen profiles.
The one crisis miss (v5-adhd-021) is a goodbye sign inside an errands request. Four of the five
decisions let through are in Spanish or French.

## How independent these results are
- **Benchmark v2: no message was used.** The loop never reads it and `classifier/clean.py` keeps
  training away from it. The loop asks for the attack kinds of `classifier/improve.py`, the attacker
  first used for v5, before v2 was built; the repository history is one squashed commit, so it cannot
  show that no kind was added later. v6.1's result on v2 is why the loop was built and why the
  false-alarm limit was raised from 2% to 5%. v2 has now been used for one promotion decision; the
  next release should be judged on a benchmark v3.
- **Test set v5 is less independent than it was.** It was drafted with Claude, and v7's loop data
  was written by a Claude model (ADR 037). Treat v5 as a regression check for this release, not as
  the headline.

## The gate sets overstated the result
The loop's own gate sets (209 messages no model trained on) showed:

| Model | Crisis recall | False crisis | Decisions let through |
|---|---|---|---|
| guard-modernbert-v6.1 | 0.781 | 0.048 | 0.100 |
| round 1 (v7) | 0.984 | 0.055 | 0.033 |
| round 2 | 0.984 | 0.069 | 0.050 |

The gate said 98.4% crisis recall; the independent benchmark says 74.9%. The gate messages are written
by the same frontier model as the training data, so they measure the loop's own style. They are still
useful for comparing rounds, and they ranked round 1 above round 2 and v6.1 correctly.

## The choice
v7 was promoted although it missed two of the loop's four gate targets narrowly (false crisis 0.055
against 0.05; dev macro F1 below 0.93), so `modelops promote` would have refused it. The decision was
made by a person on the independent benchmark: a missed crisis leaves someone unanswered, a false
alarm hands a harmless message to a person. Round 2 was not chosen: it was no better on the gate, and
its third training epoch collapsed (every dev message labelled allow; the saved weights are from
epoch 2).

## Next
- Loop data aimed at what is still missed: WildGuardTest-style adversarial wording, messages over 100
  words, goodbye signs, and means questions and decisions in Spanish and French.
- Gate messages the frontier model did not write, so the gate stops overstating progress.
- Benchmark v3 from other public sources, before another promotion.
