"""The fine-tuned guard classifier: load it and label messages.

    from classifier.model import GuardModel
    guard = GuardModel()                                   # models/guard-modernbert
    label, probs = guard.label(policy, "Can I take two Adderall?")

The label comes from the probabilities and the thresholds chosen on dev (classifier/calibrate.py,
saved as thresholds.json next to the model). Crisis is checked first, then decision; anything
else is allow. Without a thresholds file, the most likely label wins.
"""

import json
from pathlib import Path

from classifier.clean import LABELS
from classifier.train import OUT as MODEL_DIR
from classifier.train import device_name

MAX_LEN = 256


def decide(probs: dict[str, float], thresholds: dict[str, float] | None) -> str:
    """allow, decision or crisis from the probabilities. Crisis is checked first: it wins."""
    if thresholds is None:
        return max(probs, key=probs.get)
    if probs["crisis"] >= thresholds["crisis"]:
        return "crisis"
    if probs["decision"] >= thresholds["decision"]:
        return "decision"
    return "allow"


class GuardModel:
    def __init__(self, path: Path = MODEL_DIR, device: str | None = None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        if not (path / "config.json").exists():
            raise SystemExit(f"No model in {path}. Train one first: uv run --group train python -m classifier.train")
        self._torch = torch
        self.path = path
        self.device = device or device_name()
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForSequenceClassification.from_pretrained(path).to(self.device).eval()
        names = [self.model.config.id2label[i] for i in range(len(LABELS))]
        assert names == list(LABELS), f"model labels {names} do not match {LABELS}"
        saved = path / "thresholds.json"
        self.thresholds = json.loads(saved.read_text())["thresholds"] if saved.exists() else None

    def probs(self, policy: str, texts: list[str], batch: int = 32) -> list[dict[str, float]]:
        out = []
        with self._torch.no_grad():
            for i in range(0, len(texts), batch):
                chunk = texts[i:i + batch]
                enc = self.tok([policy] * len(chunk), chunk, truncation="only_first", max_length=MAX_LEN,
                               padding=True, return_tensors="pt").to(self.device)
                for row in self.model(**enc).logits.softmax(-1).cpu().tolist():
                    out.append({label: round(p, 6) for label, p in zip(LABELS, row)})
        return out

    def label(self, policy: str, text: str) -> tuple[str, dict[str, float]]:
        p = self.probs(policy, [text])[0]
        return decide(p, self.thresholds), p