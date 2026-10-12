"""Ask the ADHD planning assistant a set of everyday questions and save its answers for an expert to review.

    PROFILE=adhd-assistant ./scripts/dev.sh start        # the full system must be running
    uv run python -m eval.run_adhd_review

Each question goes through the whole pipeline (guardrails, masking, model, output checks), exactly as
a person using the chat would send it, one at a time. Nothing is scored automatically: the answers are
for a person to read. The question set (eval/datasets/adhd_review_questions.jsonl) covers the daily
difficulties adults with ADHD commonly describe (getting started, time, planning, memory, organization,
feelings, focus, work, routines, money) and a few questions the assistant must hand over (medication,
diagnosis, crisis).

Output, in eval/results/ (commit both): adhd_review_<date>.json with every answer, and a .md file
to read or share.
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

API_URL = "http://localhost:8000"
DATASET = Path("eval/datasets/adhd_review_questions.jsonl")
RESULTS = Path("eval/results")


def ask(text: str, wait: int = 300) -> dict:
    """One question through the running system; returns its reply, as the chat page gets it."""
    rid = requests.post(f"{API_URL}/chat", json={"user_id": "adhd-review", "message": text},
                        timeout=10).json()["request_id"]
    for _ in range(wait):
        data = requests.get(f"{API_URL}/chat/{rid}", timeout=10).json()
        if data["status"] == "done":
            return data
        time.sleep(1)
    raise TimeoutError(f"No answer for request {rid} after {wait}s")


def outcome(reply: dict) -> str:
    """What happened, in plain words: answered, or stopped by which check."""
    g = reply.get("guardrail")
    if not g:
        return "answered"
    return f"stopped ({g.get('category')}, {g.get('stage')} {g.get('layer')})"


def markdown(run: dict) -> str:
    lines = ["# ADHD planning assistant: answers for review\n",
             f"Profile `{run['profile']}`, run {run['run_at']}, {len(run['rows'])} questions, each sent once "
             "through the full system. Answers are shown exactly as returned.\n"]
    for r in run["rows"]:
        lines += [f"## {r['id']} · {r['category']} · expected: {r['expected']}\n",
                  f"**Question:** {r['text']}\n",
                  f"**What happened:** {r['outcome']} · {r['seconds']} s\n",
                  f"**Answer:**\n\n{r['answer']}\n"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=str(DATASET))
    args = ap.parse_args()

    profile = requests.get(f"{API_URL}/profile", timeout=10).json()["name"]
    if profile != "adhd-assistant":
        raise SystemExit(f"The running system uses profile {profile!r}. Start it with "
                         "PROFILE=adhd-assistant ./scripts/dev.sh start")
    items = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]

    rows = []
    for it in items:
        t0 = time.time()
        reply = ask(it["text"])
        row = {**it, "answer": reply.get("answer", ""), "guardrail": reply.get("guardrail"),
               "route": reply.get("route"), "sources": [s["id"] for s in reply.get("sources", [])],
               "outcome": outcome(reply), "seconds": round(time.time() - t0, 1)}
        rows.append(row)
        print(f"{it['id']}  {row['outcome']:40}  {it['text'][:60]}")

    run = {"profile": profile, "dataset": args.dataset, "run_at": datetime.now().isoformat(timespec="seconds"),
           "rows": rows}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"adhd_review_{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(run), encoding="utf-8")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in Counter(r["outcome"] for r in rows).items()))
    print(f"Saved {out} and {out.with_suffix('.md').name}. Commit both.")


if __name__ == "__main__":
    main()