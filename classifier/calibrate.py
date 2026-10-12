"""Choose the guard classifier's thresholds on DEV, and show every mistake it makes there.

    uv run --group train python -m classifier.calibrate                             # v3 (models/guard-modernbert-v3)
    uv run --group train python -m classifier.calibrate --model guard-modernbert-v2 # an earlier one

The frozen test set is never used for this (eval/datasets/guard_test_protocol.md, rule 2).

1. Crisis threshold: the LOWEST one that keeps false alarms (non-crisis messages labelled crisis)
   within MAX_FALSE_ALARMS of dev. For a safety label, the safer side (labels.md rule 8). v1 and v2
   took the highest threshold among those that caught every dev crisis message; dev's crisis
   messages were easy, so that chose 0.8 for v2, too strict for harder messages
   (eval/results/guard_test_v2_notes.md).
2. Decision threshold: with that crisis threshold, the one with the best macro F1. On a tie, the
   lower one (labels.md rule 8: when unsure, the safer side).

Saves thresholds.json next to the model, with dev scores before and after.
"""

import argparse
import json
from collections import defaultdict

from classifier.clean import DEV, LABELS, read_jsonl
from classifier.generate import POLICIES
from classifier.model import GuardModel, decide
from classifier.train import LABEL_ID, MODELS, metrics

GRID = [round(0.05 * i, 2) for i in range(1, 20)]          # 0.05 ... 0.95
MAX_FALSE_ALARMS = 0.02


def crisis_recall(gold: list[str], pred: list[str]) -> float:
    crisis = [p for g, p in zip(gold, pred) if g == "crisis"]
    return sum(p == "crisis" for p in crisis) / max(1, len(crisis))


def false_alarms(gold: list[str], pred: list[str]) -> float:
    other = [p for g, p in zip(gold, pred) if g != "crisis"]
    return sum(p == "crisis" for p in other) / max(1, len(other))


def choose(gold: list[str], probs: list[dict[str, float]]) -> dict[str, float]:
    def labels(t):
        return [decide(p, t) for p in probs]

    within = [tc for tc in GRID if false_alarms(gold, labels({"crisis": tc, "decision": 0.5})) <= MAX_FALSE_ALARMS]
    tc = min(within) if within else 0.5
    scored = []
    for td in GRID:
        pred = labels({"crisis": tc, "decision": td})
        f1 = metrics([LABEL_ID[g] for g in gold], [LABEL_ID[p] for p in pred])["macro_f1"]
        scored.append((f1, -td))
    return {"crisis": tc, "decision": -max(scored)[1]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="guard-modernbert-v3", help="folder name under models/")
    model_dir = MODELS / ap.parse_args().model
    rows = read_jsonl(DEV)
    guard = GuardModel(model_dir)
    by_profile = defaultdict(list)
    for i, r in enumerate(rows):
        by_profile[r["profile"]].append(i)
    probs = [None] * len(rows)
    for profile, idx in by_profile.items():
        for i, p in zip(idx, guard.probs(POLICIES[profile], [rows[i]["text"] for i in idx])):
            probs[i] = p

    gold = [r["label"] for r in rows]
    thresholds = choose(gold, probs)
    before = metrics([LABEL_ID[g] for g in gold], [LABEL_ID[decide(p, None)] for p in probs])
    pred = [decide(p, thresholds) for p in probs]
    after = metrics([LABEL_ID[g] for g in gold], [LABEL_ID[p] for p in pred])

    print(f"Thresholds (chosen on dev): crisis {thresholds['crisis']}, decision {thresholds['decision']}")
    for name, m in (("most likely label", before), ("with thresholds", after)):
        print(f"  {name:18} macro F1 {m['macro_f1']:.3f}  crisis recall {m['crisis']['recall']:.3f}  "
              f"allow recall {m['allow']['recall']:.3f}")
    print("\nDev mistakes with the thresholds (gold -> predicted, probabilities):")
    for r, g, p, pr in sorted(zip(rows, gold, pred, probs), key=lambda x: (x[1], x[2])):
        if g != p:
            shown = " ".join(f"{k[0]}={v:.2f}" for k, v in pr.items())
            print(f"  {g:8} -> {p:8} [{shown}] {r['profile'][:6]} · {r['angle']}: {r['text']}")

    (model_dir / "thresholds.json").write_text(json.dumps({
        "thresholds": thresholds, "chosen_on": "dev", "dev_rows": len(rows), "grid": [GRID[0], GRID[-1]],
        "max_false_alarms": MAX_FALSE_ALARMS, "dev_before": before, "dev_after": after,
        "labels": list(LABELS)}, indent=2))
    print(f"\nSaved {model_dir / 'thresholds.json'}")


if __name__ == "__main__":
    main()