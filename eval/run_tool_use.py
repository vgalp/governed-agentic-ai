"""Tool-use accuracy: does each question go to the right tool, and is the role allowed it? (ADR 018)

    uv run python -m eval.run_tool_use

Scores the planner's own routing (agents/tool_choice.py `route`: a change request first, then a
triggered tool, else the general tools) on a frozen, labelled set of staff questions
(eval/datasets/tool_use_v1.jsonl; protocol: tool_use_v1_protocol.md). No model, Kafka or OPA is
needed: routing is decided by the profile's patterns, and permissions by the same rules OPA applies.

What it measures is routing only: where a question that passed the input checks goes. Whether
the input checks block it first is measured by the guard test sets.

Rules: the set must be frozen (SHA-256 in the protocol). One run per routing configuration (the
profiles' rules, tools, roles and actions, and the routing code), so patterns cannot be tuned
against this set and scored again as if new.

Output, in eval/results/ (commit it): tool_use_v1_<config>.json with every routing, and a .md table.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from eval.run_crisis_benchmark import pct, rate

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "datasets" / "tool_use_v1.jsonl"
PROTOCOL = ROOT / "eval" / "datasets" / "tool_use_v1_protocol.md"
RESULTS = ROOT / "eval" / "results"
ROUTING_FILES = ("rules.yaml", "tools.yaml", "roles.yaml", "actions.yaml", "profile.yaml")
ROUTING_CODE = ("agents/tool_choice.py", "agents/actions.py")
KINDS = ("plain", "paraphrase", "near_miss", "role")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_hash(protocol: Path = PROTOCOL) -> str | None:
    if not protocol.exists():
        return None
    m = re.search(r"`tool_use_v1\.jsonl` SHA-256:\s*([0-9a-f]{64})", protocol.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def config_id(profiles: list[str]) -> str:
    """Identifies what decides routing: the profiles' routing files and the routing code."""
    h = hashlib.sha256()
    for p in sorted(profiles):
        for f in ROUTING_FILES:
            path = ROOT / "profiles" / p / f
            if path.exists():
                h.update(f"{p}/{f}".encode() + path.read_bytes())
    for f in ROUTING_CODE:
        h.update(f.encode() + (ROOT / f).read_bytes())
    return h.hexdigest()[:12]


def score(rows: list[dict]) -> dict:
    """Overall, by kind, and per path: precision and recall of each destination."""
    def acc(rs):
        return {"routing": rate(sum(r["path_ok"] for r in rs), len(rs)),
                "permission": rate(sum(r["allowed_ok"] for r in rs), len(rs)),
                "both": rate(sum(r["path_ok"] and r["allowed_ok"] for r in rs), len(rs))}
    paths = sorted({r["expected_path"] for r in rows} | {r["path"] for r in rows})
    per_path = {}
    for p in paths:
        tp = sum(r["path"] == p and r["expected_path"] == p for r in rows)
        predicted = sum(r["path"] == p for r in rows)
        actual = sum(r["expected_path"] == p for r in rows)
        per_path[p] = {"precision": rate(tp, predicted), "recall": rate(tp, actual)}
    return {
        "all": acc(rows),
        "by_kind": {k: acc([r for r in rows if r["kind"] == k]) for k in KINDS if any(r["kind"] == k for r in rows)},
        "by_profile": {p: acc([r for r in rows if r["profile"] == p]) for p in sorted({r["profile"] for r in rows})},
        "per_path": per_path,
        "confusion": dict(sorted(Counter(f"{r['expected_path']} -> {r['path']}" for r in rows).items())),
        # The two ways routing can go wrong, and what follows from each:
        "missed": sum(r["expected_path"] != "general" and r["path"] == "general" for r in rows),
        "false_trigger": sum(r["expected_path"] == "general" and r["path"] != "general" for r in rows),
        "wrong_permission": sum(r["path_ok"] and not r["allowed_ok"] for r in rows),
    }


def table(s: dict, rows: list[dict]) -> str:
    out = ["| Set | Questions | Right destination | Right permission | Both right |", "|---|---|---|---|---|"]
    for name, x in [("**All**", s["all"]), *[(f"kind: {k}", v) for k, v in s["by_kind"].items()],
                    *[(f"profile: {p}", v) for p, v in s["by_profile"].items()]]:
        out.append(f"| {name} | {x['both']['n']} | {pct(x['routing'])} | {pct(x['permission'])} | {pct(x['both'])} |")
    out += ["", "| Destination | Precision | Recall |", "|---|---|---|"]
    for p, v in s["per_path"].items():
        out.append(f"| {p} | {pct(v['precision'])} | {pct(v['recall'])} |")
    out += ["", f"Missed (should have gone to a tool or a change, went to the general tools): {s['missed']}. "
            f"False triggers (a general question sent to a tool or a change): {s['false_trigger']}. "
            f"Right destination, wrong permission: {s['wrong_permission']}."]
    wrong = [r for r in rows if not (r["path_ok"] and r["allowed_ok"])]
    if wrong:
        out += ["", "Wrong:", "", "| ID | Kind | Role | Question | Expected | Got |", "|---|---|---|---|---|---|"]
        for r in wrong:
            exp = f"{r['expected_path']} ({'allowed' if r['expected_allowed'] else 'denied'})"
            got = f"{r['path']} ({'allowed' if r['allowed'] else 'denied'})"
            out.append(f"| {r['id']} | {r['kind']} | {r['role']} | {r['text']} | {exp} | {got} |")
    return "\n".join(out)


def run(items: list[dict], load=None) -> list[dict]:
    from agents.tool_choice import route
    from profiles.loader import load_profile
    load = load or load_profile
    profiles, rows = {}, []
    for it in items:
        p = profiles.setdefault(it["profile"], load(it["profile"]))
        got = route(p, "planner", it["role"], it["text"])
        rows.append({**it, "path": got["path"], "allowed": got["allowed"], "tools": got["tools"],
                     "path_ok": got["path"] == it["expected_path"],
                     "allowed_ok": got["allowed"] == it["expected_allowed"]})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    want = frozen_hash()
    if want is None:
        sys.exit(f"{DATASET.name} is not frozen yet: review it and record its SHA-256 in {PROTOCOL.name} first.")
    if sha256(DATASET) != want:
        sys.exit(f"{DATASET.name} has changed since it was frozen. The protocol does not allow scoring a changed set.")
    items = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    cfg = config_id(sorted({it["profile"] for it in items}))
    out = RESULTS / f"tool_use_v1_{cfg}.json"
    if out.exists():
        sys.exit(f"Already scored with this routing configuration ({out.name}). One run per configuration.")
    rows = run(items)
    s = score(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now().isoformat(timespec="seconds")
    out.write_text(json.dumps({"set": DATASET.name, "set_sha256": want, "config": cfg, "run_at": run_at,
                               "summary": s, "rows": rows}, indent=1, ensure_ascii=False))
    md = (f"# Tool-use accuracy on {DATASET.name}\n\nRouting configuration `{cfg}`, run {run_at}. "
          f"Routing only: where a question that passed the input checks goes, and whether the role may.\n\n"
          f"{table(s, rows)}\n")
    out.with_suffix(".md").write_text(md)
    print(md)
    print(f"Saved {out.relative_to(ROOT)} and the .md table. Commit both.")


if __name__ == "__main__":
    main()
