"""Toxicity of the model's answers: a measurement for the dashboard, not a guardrail (ADR 018).

The score is Detoxify's "original" model (unitary/toxic-bert, from the Detoxify authors, Apache
2.0), run locally on the answer the model wrote: the probability, from 0 to 1, that the text is
toxic. A long answer is scored in parts and the highest part counts.

It never blocks an answer: the output rules and Llama Guard decide that, with the profile's policy.
A high score is shown in the trace and counted on the dashboard, so a person can look. It runs on
this machine, so the answer never leaves it. Only the score is kept, never the text.

Off when TOXICITY=off, or when the model cannot be loaded (it needs the train group: torch and
transformers). Then the dashboard says "not measured", never a score of 0.
"""

import os

MODEL = os.environ.get("TOXICITY_MODEL", "unitary/toxic-bert")
FLAG_AT = 0.5                # shown as "high" in the trace and counted on the dashboard
PART_CHARS = 1200            # about 300 tokens: well inside the model's 512

_model = None
_status = "not loaded"


def status() -> str:
    return _status


def load() -> bool:
    """Load the model once. False (and a reason in status()) if it is off or cannot load."""
    global _model, _status
    if _model is not None:
        return True
    if os.environ.get("TOXICITY", "on") == "off":
        _status = "off (TOXICITY=off)"
        return False
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        tok = AutoTokenizer.from_pretrained(MODEL)
        mdl = AutoModelForSequenceClassification.from_pretrained(MODEL).eval()
        labels = {i: n.lower() for i, n in mdl.config.id2label.items()}
        toxic = next(i for i, n in labels.items() if n == "toxic" or n == "toxicity")
    except Exception as e:  # noqa: BLE001 - optional: any failure means "not measured"
        _status = f"not measured ({e.__class__.__name__})"
        return False
    _model, _status = (torch, tok, mdl, toxic), "on"
    return True


def parts(text: str, size: int = PART_CHARS) -> list[str]:
    """The text in pieces of about `size` characters, split at spaces."""
    words, out, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + len(w) + 1 > size:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        out.append(cur)
    return out


def score(text: str) -> float | None:
    """Highest toxicity probability over the text's parts, or None if not measured."""
    global _status
    if not text.strip() or not load():
        return None
    torch, tok, mdl, toxic = _model
    try:
        with torch.no_grad():
            enc = tok(parts(text), truncation=True, max_length=512, padding=True, return_tensors="pt")
            probs = torch.sigmoid(mdl(**enc).logits)[:, toxic]   # multi-label model: one sigmoid per label
    except Exception as e:  # noqa: BLE001 - a measurement must never stop the answer
        _status = f"not measured ({e.__class__.__name__})"
        return None
    _status = "on"
    return round(float(probs.max()), 4)
