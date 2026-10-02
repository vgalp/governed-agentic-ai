"""Compare safety-classifier setups on the DEV set (never on frozen redteam_v1).

Setups:
  default  - Llama Guard's built-in policy (via Ollama chat)
  custom   - our deployment policy, written as Llama Guard categories (raw prompt)

Usage:
  uv run python -m eval.guard_compare --models llama-guard3:1b llama-guard3:8b
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import requests
from guardrails.classifier import classify, parse_guard_output

OLLAMA = "http://localhost:11434"
DEV_SET = Path("eval/datasets/redteam_dev.jsonl")
RESULTS_DIR = Path("eval/results")

# Our policy in Llama Guard's category format.





def run_default(model: str, text: str) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": model, "stream": False, "options": {"temperature": 0},
        "messages": [{"role": "user", "content": text}],
    }, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]


def run_custom(model: str, text: str) -> str:
    return classify(text, model=model).raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["llama-guard3:1b"])
    args = parser.parse_args()

    items = [json.loads(l) for l in DEV_SET.read_text().splitlines() if l.strip()]
    rows, table = [], []
    for model in args.models:
        for setup, fn in [("default", run_default), ("custom", run_custom)]:
            flagged = {}
            for it in items:
                raw = fn(model, it["text"])
                out = parse_guard_output(raw)
                flagged[it["id"]] = not out.safe
                rows.append({"model": model, "setup": setup, **it, "raw": raw.strip(),
                             "unsafe": not out.safe, "codes": out.codes})
                print(f"{model:18} {setup:8} {it['id']:5} {'UNSAFE ' + ','.join(out.codes) if not out.safe else 'safe'}")
            line = {"model": model, "setup": setup}
            for cat in ["crisis", "medication", "diagnosis"]:
                ids = [i["id"] for i in items if i["category"] == cat]
                line[cat] = f"{sum(flagged[i] for i in ids)}/{len(ids)}"
            normal = [i["id"] for i in items if i["expected"] == "allow"]
            line["false_flags"] = f"{sum(flagged[i] for i in normal)}/{len(normal)}"
            table.append(line)

    print("\n| Model | Setup | Crisis caught | Medication caught | Diagnosis caught | Normal wrongly flagged |")
    print("|---|---|---|---|---|---|")
    for t in table:
        print(f"| {t['model']} | {t['setup']} | {t['crisis']} | {t['medication']} | {t['diagnosis']} | {t['false_flags']} |")

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"guard_compare_dev_{stamp}.json"
    out.write_text(json.dumps({"summary": table, "rows": rows}, indent=2))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()