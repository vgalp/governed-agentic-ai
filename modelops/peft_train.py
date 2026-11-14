"""LoRA training on a CUDA machine with Hugging Face PEFT, then a merged model folder (ADR 037).

    python -m modelops.peft_train --base Qwen/Qwen3.5-9B --data <folder with train.jsonl, valid.jsonl> \\
        --out <folder> --rank 16 --alpha 32 --iters 600 --lr 1e-4

The loss is on the assistant's reply only (the prompt tokens are masked), as mlx_lm's --mask-prompt does
on Apple silicon. Writes <out>/adapter (the LoRA weights) and <out>/merged (base + adapter, safetensors),
which modelops/trainers.ollama_import serves. Needs torch, transformers and peft (not in the default
dependency groups: install them on the training machine).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_rows(path: Path) -> list[list[dict]]:
    return [json.loads(line)["messages"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def encode(tokenizer, messages: list[dict], max_len: int) -> dict:
    """Token ids for the whole conversation; labels only on the final assistant reply."""
    prompt = tokenizer.apply_chat_template(messages[:-1], tokenize=True, add_generation_prompt=True)
    full = tokenizer.apply_chat_template(messages, tokenize=True)
    full = full[:max_len]
    labels = [-100] * min(len(prompt), len(full)) + full[len(prompt):]
    return {"input_ids": full, "labels": labels[:len(full)], "attention_mask": [1] * len(full)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--iters", type=int, default=600)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--max-len", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, DataCollatorForSeq2Seq, Trainer, TrainingArguments, set_seed

    set_seed(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.base)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.bfloat16, device_map="auto")
    model = get_peft_model(model, LoraConfig(r=args.rank, lora_alpha=args.alpha, lora_dropout=0.05,
                                             target_modules="all-linear", task_type="CAUSAL_LM"))
    train = [encode(tokenizer, m, args.max_len) for m in load_rows(args.data / "train.jsonl")]
    valid = [encode(tokenizer, m, args.max_len) for m in load_rows(args.data / "valid.jsonl")]

    trainer = Trainer(
        model=model,
        args=TrainingArguments(output_dir=str(args.out / "checkpoints"), max_steps=args.iters,
                               per_device_train_batch_size=args.batch, gradient_accumulation_steps=4,
                               learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.05,
                               bf16=True, logging_steps=20, eval_strategy="steps", eval_steps=100,
                               save_strategy="no", report_to=[], seed=args.seed),
        train_dataset=train, eval_dataset=valid,
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100))
    trainer.train()
    model.save_pretrained(args.out / "adapter")
    merged = model.merge_and_unload()
    merged.save_pretrained(args.out / "merged", safe_serialization=True)
    tokenizer.save_pretrained(args.out / "merged")
    print(f"Saved {args.out / 'adapter'} and {args.out / 'merged'}")


if __name__ == "__main__":
    main()
