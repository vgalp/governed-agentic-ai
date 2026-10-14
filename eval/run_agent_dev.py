"""Try the input agent on the DEV set: rules + classifier, with and without the agent, side by side.

    uv run --group train python -m eval.run_agent_dev                    # all 418 dev messages
    uv run --group train python -m eval.run_agent_dev --limit 60         # a quick, balanced sample
    uv run --group train python -m eval.run_agent_dev --agent-model mistral --model guard-modernbert-v4

This is where the agent's prompt is developed: run it as often as you like. It reads only
classifier/data/dev.jsonl, never a frozen test set or a red-team set. The one official score is
eval.run_guard_baselines --system rules+classifier+agent on test set v5, once, after v5 is frozen.

For each message it prints the label without the agent, with the agent, and the gold label, and
then the usual measures for both, what the agent changed (fixed or broke) with the views that changed
it, its errors, how often it flagged manipulation, and the time it adds.
Output: eval/results/agent_dev/<agent>_<guard>_<time>.json (a working file: commit it only if you want
the history). It holds the view texts, which is fine for synthetic dev data and never done in the pipeline.

Needs Ollama with the agent model pulled (ollama pull mistral) and the guard classifier.
"""

import argparse
import json
import random
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from eval.run_guard_test import RESULTS, scores

DEV = Path(__file__).resolve().parent.parent / "classifier" / "data" / "dev.jsonl"
OUT = RESULTS / "agent_dev"


def sample(items: list[dict], limit: int | None, seed: int = 7) -> list[dict]:
    """All items, or about `limit` of them with the same mix of profile and label (fixed seed)."""
    if not limit or limit >= len(items):
        return items
    groups = defaultdict(list)
    for it in items:
        groups[(it["profile"], it["label"])].append(it)
    rng, out = random.Random(seed), []
    for key in sorted(groups):
        g = groups[key]
        out += rng.sample(g, max(1, round(limit * len(g) / len(items))))
    return out


def changes(rows: list[dict]) -> dict:
    """What the agent changed: fixed = now right, broke = was right, other = wrong either way."""
    changed = [r for r in rows if r["with_agent"] != r["without_agent"]]
    return {
        "changed": len(changed),
        "fixed": sum(r["with_agent"] == r["label"] for r in changed),
        "broke": sum(r["without_agent"] == r["label"] for r in changed),
        "crisis_found": sum(r["label"] == "crisis" and r["with_agent"] == "crisis" for r in changed),
        "new_wrong_blocks": sum(r["label"] == "allow" and r["without_agent"] == "allow" for r in changed),
        "agent_errors": sum(bool(r.get("agent", {}).get("agent_error")) for r in rows),
        "manipulation_flagged": sum(bool(r.get("agent", {}).get("manipulation")) for r in rows),
        "raised_by": dict(sorted(_count(r.get("agent", {}).get("raised_by") for r in changed).items())),
    }


def _count(values) -> dict:
    out: dict = defaultdict(int)
    for v in values:
        out[str(v)] += 1
    return out


def report(rows: list[dict]) -> str:
    def as_pred(key):
        return [{**r, "predicted": r[key]} for r in rows]
    without, with_ = scores(as_pred("without_agent")), scores(as_pred("with_agent"))
    c = changes(rows)
    added = [r["agent_ms"] for r in rows if "agent_ms" in r]
    lines = ["| Dev | Crisis missed | Decisions let through | Wrong blocks | Macro F1 | Everyday messages given the crisis reply |",
             "|---|---|---|---|---|---|"]
    for name, x in (("Rules + classifier", without), ("+ input agent", with_)):
        lines.append(f"| {name} | {x['crisis_missed']}/{x['crisis_total']} | {x['decisions_let_through']}/"
                     f"{x['decision_total']} | {x['wrong_blocks']}/{x['allow_total']} | {x['macro_f1']:.3f} | "
                     f"{x['false_crisis']}/{x['allow_total']} |")
    lines.append(f"\nThe agent changed {c['changed']} of {len(rows)} labels: {c['fixed']} now right, "
                 f"{c['broke']} now wrong ({c['new_wrong_blocks']} new wrong blocks). Crisis found only "
                 f"with the agent: {c['crisis_found']}. Raised by: {c['raised_by'] or 'none'}.")
    lines.append(f"Agent errors (original checked only): {c['agent_errors']}. "
                 f"Manipulation flagged: {c['manipulation_flagged']}.")
    if added:
        lines.append(f"Time the agent adds per message: median {statistics.median(added):.0f} ms, "
                     f"95th percentile {sorted(added)[max(0, int(0.95 * len(added)) - 1)]:.0f} ms.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="guard-modernbert-v4", help="guard classifier: folder under models/")
    ap.add_argument("--agent-model", default="mistral", help="local Ollama model for the input agent")
    ap.add_argument("--limit", type=int, default=None, help="a balanced sample of about this many messages")
    ap.add_argument("--profile", default=None, help="only this profile's messages")
    args = ap.parse_args()

    from classifier.generate import POLICIES
    from eval.run_guard_baselines import Runner

    items = [json.loads(line) for line in DEV.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.profile:
        items = [it for it in items if it["profile"] == args.profile]
    items = sample(items, args.limit)
    runner = Runner("rules+classifier+agent", dict(POLICIES), args.model, args.agent_model)
    runner.keep_text = True                                # synthetic dev data: show what the agent wrote
    runner.label("adhd-assistant", "Help me plan my morning.")          # warm up both models

    rows = []
    for it in items:
        without = runner.rules_classifier(it["profile"], it["text"])
        runner.extra = {}
        t0 = time.perf_counter()
        with_ = runner.label(it["profile"], it["text"])
        row = {"id": it.get("id"), "profile": it["profile"], "label": it["label"], "text": it["text"],
               "without_agent": without, "with_agent": with_, **runner.extra}
        if runner.extra:                                   # the agent ran (it is skipped after a crisis label)
            row["agent_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        rows.append(row)
        mark = "" if with_ == without else "   <- agent changed it"
        wrong = "" if with_ == it["label"] else f" (gold {it['label']})"
        print(f"{it['profile'][:14]:14} {without:8} -> {with_:8}{wrong}{mark}")

    text = report(rows)
    print("\n" + text)
    for r in rows:
        if r["with_agent"] != r["without_agent"]:
            print(f"  {r['label']:8} {r['without_agent']:8} -> {r['with_agent']:8} "
                  f"({r.get('agent', {}).get('raised_by')}): {r['text']}")
            for v in r.get("agent", {}).get("views", []):
                if v["label"] != r["without_agent"]:
                    print(f"      {v['kind']:8} {v['label']:8} {v.get('text', '')}")
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = OUT / f"{args.agent_model.replace(':', '-')}_{args.model}_{stamp}.json"
    out.write_text(json.dumps({"agent_model": args.agent_model, "guard_model": args.model,
                               "agent_config": runner.config_id(), "dev_messages": len(rows),
                               "limit": args.limit, "profile": args.profile, "run_at": stamp,
                               "changes": changes(rows), "rows": rows}, indent=2, ensure_ascii=False))
    print(f"\nSaved {out.relative_to(RESULTS.parent.parent)}")


if __name__ == "__main__":
    main()