"""The commands that train, calibrate and serve a model, run as separate processes (ADR 037).

Training runs in its own process (and its own dependency group, `uv run --group train`) so the loop
itself stays light and a crash in training cannot leave the loop half-updated. Each step is a
command list given to a `Runner`; tests pass a fake runner and nothing is executed.

Guard:   classifier.clean → classifier.train → classifier.calibrate (the existing scripts, unchanged)
Answer:  LoRA on the base weights (mlx_lm on Apple silicon, or modelops/peft_train.py on CUDA), merged
         into a full model folder, then imported into Ollama under a new tag.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[[Sequence[str]], None]
PY = [sys.executable]


def run(cmd: Sequence[str]) -> None:
    """Run one step; a failed step stops the round (the ledger keeps the last finished round)."""
    print("  $ " + " ".join(cmd), flush=True)
    subprocess.run(list(cmd), check=True)


# --- guard ------------------------------------------------------------------------------------------

def guard_steps(model_name: str, train_args: dict, seed: int = 7, max_false_alarms: float | None = None) -> list[list[str]]:
    flags = []
    for key in ("epochs", "lr", "batch", "label_smoothing", "max_len"):
        if key in train_args:
            flags += [f"--{key.replace('_', '-')}", str(train_args[key])]
    return [
        [*PY, "-m", "classifier.clean", "--seed", str(seed)],
        [*PY, "-m", "classifier.train", "--out", model_name, "--seed", str(seed), *flags],
        [*PY, "-m", "classifier.calibrate", "--model", model_name,
         *(["--max-false-alarms", str(max_false_alarms)] if max_false_alarms else [])],
    ]


# --- answer -----------------------------------------------------------------------------------------

def lora_steps(backend: str, base: str, data_dir: Path, out_dir: Path, lora: dict) -> list[list[str]]:
    """Train a LoRA adapter on data_dir/{train,valid}.jsonl (chat format) and merge it into out_dir/merged."""
    adapter, merged = out_dir / "adapter", out_dir / "merged"
    if backend == "mlx":
        return [
            [*PY, "-m", "mlx_lm", "lora", "--model", base, "--train", "--data", str(data_dir),
             "--adapter-path", str(adapter), "--iters", str(lora["iters"]), "--learning-rate", str(lora["lr"]),
             "--num-layers", str(lora.get("layers", 16)), "--batch-size", str(lora.get("batch", 4)),
             "--mask-prompt"],
            [*PY, "-m", "mlx_lm", "fuse", "--model", base, "--adapter-path", str(adapter),
             "--save-path", str(merged), "--dequantize"],
        ]
    if backend == "peft":
        return [[*PY, "-m", "modelops.peft_train", "--base", base, "--data", str(data_dir),
                 "--out", str(out_dir), "--rank", str(lora["rank"]), "--alpha", str(lora.get("alpha", 2 * lora["rank"])),
                 "--iters", str(lora["iters"]), "--lr", str(lora["lr"])]]
    raise ValueError(f"unknown backend {backend!r}")


def ollama_import(tag: str, merged: Path, quantize: str = "q4_K_M") -> tuple[str, list[str]]:
    """(Modelfile text, command) to serve the merged model under `tag`. Ollama imports safetensors
    folders for the architectures it supports; for one it does not, serve the folder with llama.cpp or
    vLLM and point the profile's model at it (provider openai_compatible)."""
    modelfile = f"FROM {merged}\nPARAMETER temperature 0\n"
    return modelfile, ["ollama", "create", tag, "-f", str(merged.parent / "Modelfile"), "--quantize", quantize]
