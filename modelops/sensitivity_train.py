"""Fine-tune ModernBERT-base to read text for sensitive categories (ADR 038 step 3).

    uv run --group train python -m modelops sensitivity train --run sens1

One model, one output per category, each yes or no on its own: a text can be in two categories or in
none. It reads the text alone (no policy beside it, unlike the guard), so it can mark every record a
tool returns in a few milliseconds each.

Trains on the run's train.jsonl (modelops/sensitivity_data.py), and on any further files it is given
(text people wrote, modelops/sensitivity_mine.py). After each epoch it scores the validation files and
keeps the best epoch (macro F1 over categories, then fewest marks on rows with no
category). Then it chooses one threshold per category on dev: the best F1 among thresholds that mark at
most `max_false_mark_rate` of the rows without the category.

Neither the run's test.jsonl nor the hand-written development set is read here. They are scored
afterwards (eval/run_sensitivity.py --classifier).

Output: models/<name>/ (not in git): the model, its tokenizer, thresholds.json and training_info.json.

The functions above `train` need no torch, so they are tested everywhere; `train` itself runs only where
the training group is installed.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

BASE = "answerdotai/ModernBERT-base"
MODELS = Path(__file__).resolve().parent.parent / "models"
THRESHOLDS = "thresholds.json"
GRID = tuple(round(0.05 * i, 2) for i in range(1, 20))        # 0.05 .. 0.95
MAX_POSITIVE_WEIGHT = 8.0
DEFAULTS = {"epochs": 4, "lr": 3e-5, "batch": 16, "max_len": 128, "seed": 7}


def label_vector(categories: Sequence[str], keys: Sequence[str]) -> list[float]:
    unknown = set(categories) - set(keys)
    if unknown:
        raise ValueError(f"row has categories the model does not learn: {', '.join(sorted(unknown))}")
    return [1.0 if k in categories else 0.0 for k in keys]


def positive_weights(rows: Sequence[dict], keys: Sequence[str], cap: float = MAX_POSITIVE_WEIGHT) -> list[float]:
    """How much more a missed category costs than a false mark, per category: rows without it over rows
    with it, capped. Most rows are in no given category, so without this the model learns to say no."""
    weights = []
    for k in keys:
        with_it = sum(k in r["categories"] for r in rows)
        if not with_it:
            raise ValueError(f"no training row has category {k}")
        weights.append(round(min(cap, max(1.0, (len(rows) - with_it) / with_it)), 3))
    return weights


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall, 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def choose_thresholds(probabilities: Sequence[Sequence[float]], gold: Sequence[Sequence[float]], keys: Sequence[str],
                      max_false_mark_rate: float) -> dict[str, float]:
    """One threshold per category: the best F1 among thresholds whose false-mark rate (over rows without the
    category) is within the limit; between equals, the one nearest 0.5. If none is within the limit, the
    strictest."""
    chosen = {}
    for j, key in enumerate(keys):
        without = sum(g[j] == 0 for g in gold)
        options = []
        for t in GRID:
            tp = sum(p[j] >= t and g[j] == 1 for p, g in zip(probabilities, gold))
            fp = sum(p[j] >= t and g[j] == 0 for p, g in zip(probabilities, gold))
            fn = sum(p[j] < t and g[j] == 1 for p, g in zip(probabilities, gold))
            if without and fp / without > max_false_mark_rate:
                continue
            options.append((_prf(tp, fp, fn)[2], -abs(t - 0.5), t))
        chosen[key] = max(options)[2] if options else GRID[-1]
    return chosen


def measure(rows: Sequence[dict], probabilities: Sequence[Sequence[float]], keys: Sequence[str],
            thresholds: dict[str, float] | None = None) -> dict:
    """Per category: precision, recall and F1. Overall: macro F1, how much implied wording was caught, and
    how many rows with no category were marked."""
    thresholds = thresholds or dict.fromkeys(keys, 0.5)
    marked = [{k for j, k in enumerate(keys) if p[j] >= thresholds[k]} for p in probabilities]
    out, f1s = {"rows": len(rows)}, []
    for k in keys:
        tp = sum(k in m and k in r["categories"] for r, m in zip(rows, marked))
        fp = sum(k in m and k not in r["categories"] for r, m in zip(rows, marked))
        fn = sum(k not in m and k in r["categories"] for r, m in zip(rows, marked))
        precision, recall, f1 = _prf(tp, fp, fn)
        out[k] = {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), "rows": tp + fn}
        f1s.append(f1)
    out["macro_f1"] = round(sum(f1s) / len(f1s), 4)
    implied = [(r, m) for r, m in zip(rows, marked) if r["stratum"] == "implicit"]
    out["implied_caught"] = round(sum(set(r["categories"]) <= m for r, m in implied) / max(1, len(implied)), 4)
    empty = [m for r, m in zip(rows, marked) if not r["categories"]]
    out["marked_without_any"] = round(sum(bool(m) for m in empty) / max(1, len(empty)), 4)
    return out


def better(new: dict, best: dict | None) -> bool:
    """Higher macro F1 wins; on a tie, fewer marks on rows with no category."""
    return best is None or (new["macro_f1"], -new["marked_without_any"]) > (best["macro_f1"], -best["marked_without_any"])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def train(train_files: Sequence[Path], dev_files: Sequence[Path], out_dir: Path, keys: Sequence[str],
          max_false_mark_rate: float, settings: dict) -> dict:
    """Train on every row of `train_files`, choose the epoch and thresholds on `dev_files`, save to out_dir.
    Returns what is written to training_info.json."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup

    from classifier.train import device_name

    s = {**DEFAULTS, **settings}
    keys = list(keys)
    rows = [r for f in train_files for r in read_rows(f)]
    dev = [r for f in dev_files for r in read_rows(f)]
    random.seed(s["seed"])
    torch.manual_seed(s["seed"])
    device = device_name()
    weights = positive_weights(rows, keys)
    print(f"train {len(rows)}, dev {len(dev)}, device {device}, base {BASE}, {len(keys)} categories")

    tok = AutoTokenizer.from_pretrained(BASE)
    model = AutoModelForSequenceClassification.from_pretrained(
        BASE, num_labels=len(keys), id2label=dict(enumerate(keys)), label2id={k: i for i, k in enumerate(keys)},
        problem_type="multi_label_classification")
    if hasattr(model.config, "reference_compile"):       # older transformers: torch.compile fails on Apple GPUs
        model.config.reference_compile = False
    model.to(device)

    def batches(items: list[dict], shuffle: bool):
        items = items[:]
        if shuffle:
            random.shuffle(items)
        for i in range(0, len(items), s["batch"]):
            chunk = items[i:i + s["batch"]]
            enc = tok([c["text"] for c in chunk], truncation=True, max_length=s["max_len"], padding=True,
                      return_tensors="pt")
            labels = torch.tensor([label_vector(c["categories"], keys) for c in chunk], device=device)
            yield {k: v.to(device) for k, v in enc.items()}, labels

    def probabilities(items: list[dict]) -> list[list[float]]:
        model.eval()
        out: list[list[float]] = []
        with torch.no_grad():
            for enc, _ in batches(items, shuffle=False):
                out += torch.sigmoid(model(**enc).logits.float()).tolist()
        return out

    steps = s["epochs"] * ((len(rows) + s["batch"] - 1) // s["batch"])
    opt = torch.optim.AdamW(model.parameters(), lr=s["lr"], weight_decay=0.01)
    sched = get_linear_schedule_with_warmup(opt, int(0.1 * steps), steps)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(weights, device=device))
    best, best_probabilities, history = None, [], []
    for epoch in range(1, s["epochs"] + 1):
        model.train()
        t0, total, n = time.time(), 0.0, 0
        for enc, labels in batches(rows, shuffle=True):
            loss = loss_fn(model(**enc).logits.float(), labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            total, n = total + loss.item(), n + 1
        scored = probabilities(dev)
        scores = measure(dev, scored, keys)
        history.append({"epoch": epoch, "train_loss": round(total / n, 4), "dev": scores})
        print(f"epoch {epoch}: loss {total / n:.4f}  dev macro F1 {scores['macro_f1']:.3f}  implied caught "
              f"{scores['implied_caught']:.3f}  marked without any {scores['marked_without_any']:.3f}  "
              f"({time.time() - t0:.0f}s)")
        if better(scores, best):
            best, best_probabilities = scores, scored
            out_dir.mkdir(parents=True, exist_ok=True)
            model.save_pretrained(out_dir)
            tok.save_pretrained(out_dir)
            print(f"  saved to {out_dir}")

    gold = [label_vector(r["categories"], keys) for r in dev]
    thresholds = choose_thresholds(best_probabilities, gold, keys, max_false_mark_rate)
    (out_dir / THRESHOLDS).write_text(json.dumps({"categories": keys, "thresholds": thresholds,
                                                  "max_len": s["max_len"]}, indent=1) + "\n")
    info = {
        "base_model": BASE, "categories": keys, "trained": date.today().isoformat(), "settings": s, "device": device,
        "positive_weights": dict(zip(keys, weights)), "max_false_mark_rate": max_false_mark_rate,
        "data": {"train": [{"file": f.name, "rows": len(read_rows(f)), "sha256": sha256(f)} for f in train_files],
                 "dev": [{"file": f.name, "rows": len(read_rows(f)), "sha256": sha256(f)} for f in dev_files]},
        "best_dev_at_0.5": best, "thresholds": thresholds,
        "dev_at_thresholds": measure(dev, best_probabilities, keys, thresholds), "history": history,
        "note": "Dev only, and dev comes from the same sources as the training text. The test sets and the "
                "hand-written development set are scored separately (eval/run_sensitivity.py --classifier).",
    }
    (out_dir / "training_info.json").write_text(json.dumps(info, indent=2) + "\n")
    at = info["dev_at_thresholds"]
    print(f"Best dev at the chosen thresholds: macro F1 {at['macro_f1']:.3f}, implied caught {at['implied_caught']:.3f}, "
          f"marked without any {at['marked_without_any']:.3f}")
    print("Thresholds:", thresholds)
    return info
