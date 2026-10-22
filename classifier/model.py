"""The fine-tuned guard classifier: load it and label messages.

    from classifier.model import GuardModel
    guard = GuardModel()                                   # models/guard-modernbert
    label, probs = guard.label(policy, "Can I take two Adderall?")

The label comes from the probabilities and the thresholds chosen on dev (classifier/calibrate.py,
saved as thresholds.json next to the model). Crisis is checked first, then decision; anything
else is allow. Without a thresholds file, the most likely label wins.

Long messages: the model reads the policy and the message together, at most MAX_LEN tokens. The
policy is always kept whole. A message that does not fit in the room left is split into
overlapping pieces; each piece is read with the whole policy, and the strictest result counts
(highest crisis and decision probabilities). A message that fits is read exactly as before. Until
2026-10-22 the policy was cut to make room instead, and a message longer than MAX_LEN on its own
could not be labelled at all (the pipeline then blocked it as "guardrail unavailable").
"""

import json
from pathlib import Path

from classifier.clean import LABELS
from classifier.train import OUT as MODEL_DIR
from classifier.train import device_name

MAX_LEN = 256
OVERLAP = 32          # tokens shared by neighbouring pieces of a long message
MIN_ROOM = 48         # fewer tokens than this left for the message: the policy is too long


def pieces(ids: list[int], room: int, overlap: int = OVERLAP) -> list[list[int]]:
    """A message's token ids in pieces of at most `room`, each sharing `overlap` with the next."""
    if len(ids) <= room:
        return [ids]
    step = room - overlap
    out = []
    for start in range(0, len(ids), step):
        out.append(ids[start:start + room])
        if start + room >= len(ids):
            break
    return out


def strictest(probs: list[dict[str, float]]) -> dict[str, float]:
    """One result for several pieces of a message: the highest crisis and decision probabilities."""
    if len(probs) == 1:
        return probs[0]
    return {"allow": min(p["allow"] for p in probs), "decision": max(p["decision"] for p in probs),
            "crisis": max(p["crisis"] for p in probs)}


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

    def room(self, policy: str) -> int:
        """Tokens left for the message once the whole policy and the special tokens are in."""
        used = len(self.tok(policy, add_special_tokens=False)["input_ids"]) + self.tok.num_special_tokens_to_add(pair=True)
        if MAX_LEN - used < MIN_ROOM:
            raise ValueError(f"the guard policy is too long: {used} of {MAX_LEN} tokens, leaving under "
                             f"{MIN_ROOM} for the message")
        return MAX_LEN - used

    def split(self, policy: str, text: str) -> list[str]:
        """The message, or its overlapping pieces if it does not fit beside the whole policy."""
        ids = self.tok(text, add_special_tokens=False)["input_ids"]
        parts = pieces(ids, self.room(policy))
        return [text] if len(parts) == 1 else [self.tok.decode(p) for p in parts]

    def probs(self, policy: str, texts: list[str], batch: int = 32) -> list[dict[str, float]]:
        owner, parts = [], []
        for i, text in enumerate(texts):
            for part in self.split(policy, text):
                owner.append(i)
                parts.append(part)
        raw = []
        with self._torch.no_grad():
            for i in range(0, len(parts), batch):
                chunk = parts[i:i + batch]
                # only_second: never cut the policy. Messages fit already; this only absorbs a token
                # or two that decoding a piece and reading it again can add.
                enc = self.tok([policy] * len(chunk), chunk, truncation="only_second", max_length=MAX_LEN,
                               padding=True, return_tensors="pt").to(self.device)
                for row in self.model(**enc).logits.softmax(-1).cpu().tolist():
                    raw.append({label: round(p, 6) for label, p in zip(LABELS, row)})
        grouped: list[list[dict[str, float]]] = [[] for _ in texts]
        for i, p in zip(owner, raw):
            grouped[i].append(p)
        return [strictest(g) for g in grouped]

    def label(self, policy: str, text: str) -> tuple[str, dict[str, float]]:
        p = self.probs(policy, [text])[0]
        return decide(p, self.thresholds), p