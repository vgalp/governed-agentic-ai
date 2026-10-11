"""Fine-tune ModernBERT-base as the guard classifier (allow / decision / crisis).

    uv run --group train python -m classifier.train                       # v2: label smoothing, own folder
    uv run --group train python -m classifier.train --out guard-modernbert --label-smoothing 0   # as v1

Input to the model: the profile's guard policy and the message, as a pair, so one model serves
every profile and a new profile only needs a new policy (docs/classifier/labels.md).

Trains on classifier/data/train.jsonl. After each epoch it scores classifier/data/dev.jsonl and
keeps the best epoch (macro F1, then crisis recall). The frozen test set is NOT used here: it is
scored once, by a separate script, after training is finished.

Label smoothing (v2): the model is trained towards 90% sure instead of 100%, so its probabilities
stay meaningful and the thresholds chosen on dev can do their job. v1 (no smoothing) was nearly
always 0% or 100% sure, even when wrong.

Output: models/<--out>/ (not in git, default guard-modernbert-v2; v1 is in guard-modernbert): the
model, its tokenizer, and training_info.json (base model, data hashes, settings, dev scores).
"""

import argparse
import hashlib
import json
import random
import time
from collections import Counter
from datetime import date
from pathlib import Path

from classifier.clean import DEV, LABELS, TRAIN, read_jsonl
from classifier.generate import POLICIES

BASE = "answerdotai/ModernBERT-base"
MODELS = Path(__file__).resolve().parent.parent / "models"
OUT = MODELS / "guard-modernbert"                   # v1, the default for loading
LABEL_ID = {label: i for i, label in enumerate(LABELS)}


def examples(path: Path) -> list[tuple[str, str, int]]:
    """(policy, message, label id) for every row."""
    return [(POLICIES[r["profile"]], r["text"], LABEL_ID[r["label"]]) for r in read_jsonl(path)]


def metrics(gold: list[int], pred: list[int]) -> dict:
    """Accuracy, macro F1, and precision / recall / F1 per label. Crisis recall matters most:
    a missed crisis message is the worst mistake the guard can make."""
    out, f1s = {"n": len(gold), "accuracy": round(sum(g == p for g, p in zip(gold, pred)) / max(1, len(gold)), 4)}, []
    for i, label in enumerate(LABELS):
        tp = sum(g == i and p == i for g, p in zip(gold, pred))
        fp = sum(g != i and p == i for g, p in zip(gold, pred))
        fn = sum(g == i and p != i for g, p in zip(gold, pred))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[label] = {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}
        f1s.append(f1)
    out["macro_f1"] = round(sum(f1s) / len(f1s), 4)
    out["confusion"] = {f"{LABELS[g]}->{LABELS[p]}": n for (g, p), n in sorted(Counter(zip(gold, pred)).items())}
    return out


def better(new: dict, best: dict | None) -> bool:
    """Higher macro F1 wins; on a tie, higher crisis recall."""
    if best is None:
        return True
    return (new["macro_f1"], new["crisis"]["recall"]) > (best["macro_f1"], best["crisis"]["recall"])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def device_name() -> str:
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    return "cuda" if torch.cuda.is_available() else "cpu"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=256)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--label-smoothing", type=float, default=0.1)
    ap.add_argument("--out", default="guard-modernbert-v2", help="folder name under models/")
    args = ap.parse_args()
    out_dir = MODELS / args.out

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = device_name()
    train, dev = examples(TRAIN), examples(DEV)
    print(f"train {len(train)}, dev {len(dev)}, device {device}, base {BASE}")

    tok = AutoTokenizer.from_pretrained(BASE)
    model = AutoModelForSequenceClassification.from_pretrained(
        BASE, num_labels=len(LABELS), id2label=dict(enumerate(LABELS)), label2id=LABEL_ID,
    )
    if hasattr(model.config, "reference_compile"):       # older transformers: torch.compile fails on Apple GPUs
        model.config.reference_compile = False
    model.to(device)

    def batches(rows, shuffle):
        rows = rows[:]
        if shuffle:
            random.shuffle(rows)
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            enc = tok([c[0] for c in chunk], [c[1] for c in chunk], truncation="only_first",
                      max_length=args.max_len, padding=True, return_tensors="pt")
            yield {k: v.to(device) for k, v in enc.items()}, torch.tensor([c[2] for c in chunk], device=device)

    def predict(rows):
        model.eval()
        out = []
        with torch.no_grad():
            for enc, _ in batches(rows, shuffle=False):
                out += model(**enc).logits.argmax(-1).tolist()
        return out

    steps = args.epochs * ((len(train) + args.batch - 1) // args.batch)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    sched = get_linear_schedule_with_warmup(opt, int(0.1 * steps), steps)
    loss_fn = torch.nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)
    best, history = None, []
    for epoch in range(1, args.epochs + 1):
        model.train()
        t0, total, n = time.time(), 0.0, 0
        for enc, labels in batches(train, shuffle=True):
            loss = loss_fn(model(**enc).logits, labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            total, n = total + loss.item(), n + 1
        scores = metrics([r[2] for r in dev], predict(dev))
        history.append({"epoch": epoch, "train_loss": round(total / n, 4), "dev": scores})
        print(f"epoch {epoch}: loss {total / n:.4f}  dev macro F1 {scores['macro_f1']:.3f}  "
              f"crisis recall {scores['crisis']['recall']:.3f}  ({time.time() - t0:.0f}s)")
        if better(scores, best):
            best = scores
            out_dir.mkdir(parents=True, exist_ok=True)
            model.save_pretrained(out_dir)
            tok.save_pretrained(out_dir)
            print(f"  saved to {out_dir}")

    info = {
        "base_model": BASE, "labels": list(LABELS), "trained": date.today().isoformat(),
        "settings": vars(args), "device": device,
        "data": {"train": {"rows": len(train), "sha256": sha256(TRAIN)},
                 "dev": {"rows": len(dev), "sha256": sha256(DEV)}},
        "best_dev": best, "history": history,
        "note": "Dev only. The frozen test set is scored separately, once, after training.",
    }
    (out_dir / "training_info.json").write_text(json.dumps(info, indent=2))
    print(f"Best dev: macro F1 {best['macro_f1']:.3f}, crisis recall {best['crisis']['recall']:.3f}")
    print("Confusion (gold->predicted):", best["confusion"])


if __name__ == "__main__":
    main()