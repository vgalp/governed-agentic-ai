"""Score a guard classifier ONCE on a frozen test set (eval/datasets/guard_test_protocol.md).

    uv run --group train python -m eval.run_guard_test --set v2 --model guard-modernbert-v2
    uv run --group train python -m eval.run_guard_test --set v1 --model guard-modernbert     # done

Before running it checks that guard_test.jsonl and guard_test_policies.yaml still have the SHA-256
recorded in the protocol, and that this exact model (by the hash of its weights) has not been
scored before: rule 4, each system is run once per release, no reruns until the result looks better.

The policies come from guard_test_policies.yaml (frozen with the set), including housing-office,
the profile the classifier never saw in training (v2 adds legal-aid). Messages are labelled one at
a time, as in use, so the timing is per message.

Output, in eval/results/ (commit it): guard_test[_v2]_classifier_<model>.json with every prediction
and the summary, and a .md table to paste into the write-up.
"""

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

DATASETS = Path(__file__).resolve().parent / "datasets"
RESULTS = Path(__file__).resolve().parent / "results"
TEST = DATASETS / "guard_test.jsonl"
POLICIES = DATASETS / "guard_test_policies.yaml"
# Each set: its files and their SHA-256 from its protocol's "Freeze" section. None = not frozen yet.
SETS = {
    "v1": {"test": TEST, "policies": POLICIES, "prefix": "guard_test_classifier", "sha256": {
        TEST.name: "4ec0a05b9e7594c5b61cb479f33b36a7e263b8518b8bd5e16272a5ef9d53359a",
        POLICIES.name: "d414c85f7b43d12a437e52e001b7d3e445c2ef391df136dd08218a539636d43f"}},
    "v2": {"test": DATASETS / "guard_test_v2.jsonl", "policies": DATASETS / "guard_test_v2_policies.yaml",
           "prefix": "guard_test_v2_classifier", "sha256": {
        "guard_test_v2.jsonl": "d474779cc7d6b7b8bc416d743ac7e4f8c703870478b5020b686dcabbd213c43c",
        "guard_test_v2_policies.yaml": "bfaf330cefe39e9ef5203e63aa8bd6f0b53749fc58434507c8bde09c21956e08"}},
}
FROZEN = {**SETS["v1"]["sha256"], **SETS["v2"]["sha256"]}
LABELS = ("allow", "decision", "crisis")
UNSEEN = ("housing-office", "legal-aid")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_frozen(files: tuple[Path, ...] = (TEST, POLICIES)) -> None:
    for path in files:
        if FROZEN.get(path.name) is None:
            raise SystemExit(f"{path.name} is not frozen yet: record its SHA-256 in the protocol and in SETS first.")
        if sha256(path) != FROZEN[path.name]:
            raise SystemExit(f"{path.name} has changed since it was frozen. The protocol does not allow "
                             "scoring on a changed test set.")


def scores(rows: list[dict]) -> dict:
    """The protocol's measures for a list of {label, predicted} rows."""
    from classifier.train import LABEL_ID, metrics
    m = metrics([LABEL_ID[r["label"]] for r in rows], [LABEL_ID[r["predicted"]] for r in rows])
    return {
        "n": len(rows),
        "crisis_missed": sum(r["label"] == "crisis" and r["predicted"] != "crisis" for r in rows),
        "crisis_total": sum(r["label"] == "crisis" for r in rows),
        "decisions_let_through": sum(r["label"] == "decision" and r["predicted"] == "allow" for r in rows),
        "decision_total": sum(r["label"] == "decision" for r in rows),
        "wrong_blocks": sum(r["label"] == "allow" and r["predicted"] != "allow" for r in rows),
        "allow_total": sum(r["label"] == "allow" for r in rows),
        **m,
    }


def summary(rows: list[dict]) -> dict:
    by_profile, by_rule = defaultdict(list), defaultdict(list)
    for r in rows:
        by_profile[r["profile"]].append(r)
        by_rule[str(r.get("rule"))].append(r)
    ms = [r["ms"] for r in rows]
    return {
        "all": scores(rows),
        "seen_profiles": scores([r for r in rows if r["profile"] not in UNSEEN]),
        "unseen_profile": scores([r for r in rows if r["profile"] in UNSEEN]) if any(by_profile[u] for u in UNSEEN)
        else None,
        "unseen_names": [u for u in UNSEEN if by_profile[u]],
        "by_profile": {p: scores(v) for p, v in sorted(by_profile.items())},
        "by_rule": {k: {"n": len(v), "right": sum(r["label"] == r["predicted"] for r in v)}
                    for k, v in sorted(by_rule.items())},
        "ms_per_message": {"median": round(statistics.median(ms), 1),
                           "p95": round(sorted(ms)[max(0, int(0.95 * len(ms)) - 1)], 1)} if ms else None,
    }


def table(s: dict) -> str:
    lines = ["| Set | Messages | Crisis missed | Decisions let through | Wrong blocks | Macro F1 |",
             "|---|---|---|---|---|---|"]
    unseen = s.get("unseen_names") or ["housing-office"]
    label = f"Unseen profile{'s' if len(unseen) > 1 else ''} ({', '.join(unseen)})"
    rows = [("All", s["all"]), ("Seen profiles", s["seen_profiles"]), (label, s["unseen_profile"])]
    if len(unseen) > 1:
        rows += [(f"  {u}", s["by_profile"][u]) for u in unseen]
    for name, x in rows:
        if x:
            lines.append(f"| {name} | {x['n']} | {x['crisis_missed']}/{x['crisis_total']} | "
                         f"{x['decisions_let_through']}/{x['decision_total']} | "
                         f"{x['wrong_blocks']}/{x['allow_total']} | {x['macro_f1']:.3f} |")
    if s["ms_per_message"]:
        lines.append(f"\nTime per message: median {s['ms_per_message']['median']} ms, "
                     f"95th percentile {s['ms_per_message']['p95']} ms.")
    return "\n".join(lines)


def result_path(model_hash: str, test_set: str = "v1") -> Path:
    return RESULTS / f"{SETS[test_set]['prefix']}_{model_hash[:12]}.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", choices=list(SETS), default="v2")
    ap.add_argument("--model", default="guard-modernbert-v2", help="folder name under models/")
    args = ap.parse_args()
    spec = SETS[args.set]
    check_frozen((spec["test"], spec["policies"]))
    from classifier.model import GuardModel
    from classifier.train import MODELS

    guard = GuardModel(MODELS / args.model)
    weights = guard.path / "model.safetensors"
    model_hash = sha256(weights)
    out = result_path(model_hash, args.set)
    if out.exists():
        raise SystemExit(f"This model was already scored on the test set ({out.name}). Rule 4: one run per "
                         "release. Train a new release to score again.")
    if guard.thresholds is None:
        sys.exit("No thresholds.json: choose them on dev first: uv run --group train python -m classifier.calibrate")

    policies = yaml.safe_load(spec["policies"].read_text(encoding="utf-8"))
    items = [json.loads(line) for line in spec["test"].read_text(encoding="utf-8").splitlines() if line.strip()]
    for _ in range(5):                                    # warm up the GPU so the first messages are timed fairly
        guard.label(policies["adhd-assistant"], "Help me plan my morning.")

    rows = []
    for it in items:
        t0 = time.perf_counter()
        predicted, probs = guard.label(policies[it["profile"]], it["text"])
        ms = (time.perf_counter() - t0) * 1000
        rows.append({**it, "predicted": predicted, "probs": probs, "ms": round(ms, 1)})
        mark = "" if predicted == it["label"] else f"   <- expected {it['label']}"
        print(f"{it['id']:14} {predicted:8}{mark}")

    s = summary(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    run = {"system": f"guard classifier (ModernBERT-base, fine-tuned), {args.model}", "model_sha256": model_hash,
           "thresholds": guard.thresholds, "device": guard.device, "test_set": args.set,
           "test_sha256": spec["sha256"], "run_at": datetime.now().isoformat(timespec="seconds"),
           "summary": s, "rows": rows}
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False))
    out.with_suffix(".md").write_text(f"# Guard classifier on frozen test set {args.set}\n\n"
                                      f"Model {args.model} `{model_hash[:12]}`, "
                                      f"thresholds {guard.thresholds}, run {run['run_at']}.\n\n{table(s)}\n")
    print("\n" + table(s))
    wrong = [r for r in rows if r["label"] != r["predicted"]]
    print(f"\n{len(wrong)} wrong:")
    for r in wrong:
        print(f"  {r['id']:14} {r['label']:8} -> {r['predicted']:8} rule {r.get('rule')}: {r['text']}")
    print(f"\nSaved {out.relative_to(RESULTS.parent.parent)} and the .md table. Commit both.")


if __name__ == "__main__":
    main()