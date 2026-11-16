"""The training loop's commands (ADR 037).

    uv run --group train python -m modelops guard run --run nov26            # rounds until a stop condition
    uv run python -m modelops answer select --run nov26                      # compare the candidate models
    uv run python -m modelops answer run --run nov26                         # rounds on the chosen base
    uv run python -m modelops status --run nov26                             # every round so far
    uv run python -m modelops sensitivity data --run sens1                   # text for the sensitive-category classifier
    uv run --group train python -m modelops sensitivity train --run sens1    # train it (ADR 038 step 3)
    uv run python -m modelops promote guard --run nov26                      # sealed checks for the best guard
    uv run python -m modelops promote answer --run nov26                     # sealed checks for the best answer model

Runs in the foreground and can be stopped at any time: a round is recorded only when it has finished,
and the next `run` with the same --run continues from there (frontier replies come from the cache).
The frontier key comes from the environment (ANTHROPIC_API_KEY for the default model).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

from modelops import config
from modelops.ledger import RUNS, Ledger

ROOT = Path(__file__).resolve().parent.parent


def _frontier(cfg: config.LoopConfig, ledger: Ledger):
    import os

    from modelops.frontier import Frontier
    env = cfg.frontier["api_key_env"]
    if not os.environ.get(env):
        raise SystemExit(f"Set {env} first (export {env}=...): the frontier model writes and labels the data.")
    return Frontier(cfg.frontier, cfg.budget.max_usd, ledger.dir)


def _reviewer(cfg: config.LoopConfig, ledger: Ledger, frontier):
    if cfg.reviewer is None:
        return frontier
    from modelops.frontier import Frontier
    return Frontier(cfg.reviewer, cfg.budget.max_usd, ledger.dir)


def compare_guards(ledger: Ledger, names: list[str], load=None, dev_f1=None) -> list[dict]:
    """Every named guard on every gate set of the run: messages none of them trained on, so a fair
    comparison with the guard in use. Local only; no frontier calls."""
    from modelops import guard_loop
    load = load or guard_loop._load_guard              # noqa: SLF001
    dev_f1 = dev_f1 or _dev_f1_or_none
    rows = [r for f in sorted(guard_loop.clean.GATE.glob(f"{ledger.run_id}_r*.jsonl")) for r in guard_loop._read_jsonl(f)]
    if not rows:
        raise SystemExit(f"Run {ledger.run_id} has no gate set yet: finish a round first.")
    out = []
    for name in names:
        m = guard_loop.measure(rows, load(name), dev_f1(name) or 0.0)
        out.append({"model": name, "crisis_recall": m.crisis_recall, "false_crisis": m.false_crisis,
                    "decision_leak": m.decision_leak, "gate_messages": m.gate_messages, "score": m.score()})
    return out


def _dev_f1_or_none(name: str) -> float | None:
    from modelops.guard_loop import _dev_f1
    try:
        return _dev_f1(name)
    except (FileNotFoundError, KeyError, ValueError):
        return None


def current_guard(profile: str = "clinic-assistant") -> str:
    """The guard the profiles use now: the baseline a round has to beat."""
    from profiles.loader import load_profile
    return load_profile(profile).classifier.guard_model


def cmd_guard(args, cfg) -> int:
    from modelops.guard_loop import GuardLoop
    ledger = Ledger(args.run)
    if args.action == "compare":
        names = args.model or [current_guard(), *[r["model_name"] for r in ledger.rounds("guard")]]
        results = compare_guards(ledger, names)
        print(f"Gate sets of run {args.run}: {results[0]['gate_messages']} messages no model trained on\n")
        print(f"{'model':40} {'crisis recall':>14} {'false crisis':>13} {'decision let through':>21}")
        for r in results:
            print(f"{r['model']:40} {r['crisis_recall']:14.3f} {r['false_crisis']:13.3f} {r['decision_leak']:21.3f}")
        t = cfg.guard.targets
        print(f"{'targets':40} {'>= ' + str(t.crisis_recall):>14} {'<= ' + str(t.false_crisis):>13} "
              f"{'<= ' + str(t.decision_leak):>21}")
        print("\nThe gate sets were written by the same frontier model as the training data: this compares models, "
              "it does not replace the sealed benchmarks at promotion.")
        return 0
    frontier = _frontier(cfg, ledger)
    if args.action == "review":
        return review_backlog(cfg, ledger, _reviewer(cfg, ledger, frontier))
    print(GuardLoop(cfg, frontier, ledger, review_with=_reviewer(cfg, ledger, frontier)).run(args.rounds))
    return 0


def review_backlog(cfg, ledger: Ledger, frontier) -> int:
    """Send rows already waiting in audit.csv (rounds run before the reviewer existed) to the reviewer. What it
    settles is recorded in reviewed.csv and added to the training data for the next round; audit.csv keeps
    only what it is unsure about."""
    from classifier.generate import OUT as RAW
    from modelops import reviewer
    from modelops.guard_loop import GuardLoop
    total_settled = total_left = 0
    for audit in sorted((ledger.dir / "guard").glob("round_*/audit.csv")):
        rows = reviewer.read_csv(audit)
        open_rows = [r for r in rows if not r.get("your_label")]
        pending = [r for r in open_rows if not r.get("reviewer_label")]          # never reviewed, or unreadable
        stored = [r for r in open_rows if r.get("reviewer_label")]               # reviewed: decide again, free
        if not open_rows:
            continue
        done = reviewer.review(frontier, pending, f"backlog:{audit.parent.name}") if pending else \
            {"accepted": [], "rejected": [], "flagged": []}
        for r in stored:
            verdict, decided = reviewer.decide(r, {"label": r["reviewer_label"], "confidence": r["reviewer_confidence"],
                                                   "reason": r.get("concern"), "needs_person": r.get("needs_person")})
            done[verdict].append(decided)
        n = int(audit.parent.name.split("_")[1])
        from modelops import sanitize
        kept, _ = sanitize.guard_rows([{**r, "angle": f"loop_{r['kind']}", "generator": "backlog"}
                                       for r in done["accepted"]])
        GuardLoop._append_raw_rows(RAW, kept, ledger.run_id, n)
        earlier = reviewer.read_csv(audit.parent / "reviewed.csv") if (audit.parent / "reviewed.csv").exists() else []
        reviewer.write_csv(audit.parent / "reviewed.csv", earlier + done["accepted"] + done["rejected"])
        left = [r for r in rows if r.get("your_label")] + done["flagged"]
        reviewer.write_csv(audit, left)
        total_settled += len(done["accepted"]) + len(done["rejected"])
        total_left += len(done["flagged"])
        from collections import Counter
        why = Counter(r["why"] for r in done["flagged"])
        print(f"{audit.parent.name}: {len(done['accepted'])} settled and added to training, {len(done['rejected'])} "
              f"dropped, {len(done['flagged'])} left for you" + (f" ({dict(why)})" if why else ""))
    print(f"Reviewer settled {total_settled}; {total_left} need a person (audit.csv, column your_label).")
    return 0


def cmd_answer(args, cfg) -> int:
    from modelops.answer_loop import AnswerLoop
    ledger = Ledger(args.run)
    loop = AnswerLoop(cfg, _frontier(cfg, ledger), ledger)
    print(loop.select() if args.action == "select" else loop.run(args.rounds, args.base))
    return 0


def sensitivity_model_name(run_id: str) -> str:
    return f"sensitivity-modernbert-{run_id}"


def sensitivity_data(cfg: config.LoopConfig, ledger: Ledger, writer, labeller) -> dict:
    """Write the run's test set (the reviewer model writes, the frontier model labels), then its training
    text (the other way round), kept away from the test set and the hand-written development set."""
    from eval.run_sensitivity import DEV
    from modelops import sensitivity_data as data
    from privacy import sensitivity
    sets, s, out = sensitivity.load(), cfg.sensitivity, ledger.dir / "sensitivity"
    held = data.held_out_texts(DEV)
    print("Test set:")
    test, test_report = data.build(labeller, writer, sets, data.plan(sets, s.test_batches_per_cell, seed=11),
                                   f"{ledger.run_id}-test", held)
    print("Training text:")
    rows, train_report = data.build(writer, labeller, sets, data.plan(sets, s.train_batches_per_cell, seed=7),
                                    f"{ledger.run_id}-train", held + [r["text"] for r in test])
    train, dev = data.split(rows, s.dev_share)
    for name, part in (("test", test), ("train", train), ("dev", dev)):
        data.write_jsonl(out / f"{name}.jsonl", part)
    report = {"value_sets_sha256": sets.sha256, "train": {**train_report, "train_rows": len(train), "dev_rows": len(dev)},
              "test": test_report}
    (out / "data_report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def cmd_sensitivity(args, cfg) -> int:
    ledger = Ledger(args.run)
    out, name = ledger.dir / "sensitivity", sensitivity_model_name(args.run)
    if args.action == "data":
        frontier = _frontier(cfg, ledger)
        reviewer = _reviewer(cfg, ledger, frontier)
        if reviewer is frontier:
            print("loop.yaml has no reviewer block: one model would write and label everything, and be tested on "
                  "its own writing. Add a reviewer (a different model) first.")
            return 2
        report = sensitivity_data(cfg, ledger, frontier, reviewer)
        for part in ("train", "test"):
            r = report[part]
            print(f"\n{part}: {r['kept']} kept of {r['texts_written']} written "
                  f"(writer {r['writer']}, blind labels {r['labeller']})\n"
                  f"  writer and labeller disagreed: {r['writer_and_labeller_disagreed']}\n"
                  f"  dropped in cleaning: {r['dropped_in_cleaning']}\n"
                  f"  by kind: {r['by_stratum']}\n  by category: {r['by_category']}")
        print(f"\nSaved {out.relative_to(ROOT)}/train.jsonl, dev.jsonl, test.jsonl and data_report.json. "
              f"Read a page of each before training.\nNext: uv run --group train python -m modelops sensitivity "
              f"train --run {args.run}")
        return 0
    if not (out / "train.jsonl").exists():
        print(f"Run {args.run} has no training text yet: python -m modelops sensitivity data --run {args.run}")
        return 1
    from modelops import sensitivity_train
    from privacy import sensitivity
    from privacy.sensitivity_model import readable_categories
    keys = list(readable_categories(sensitivity.load()))
    sensitivity_train.train(out, sensitivity_train.MODELS / name, keys, cfg.sensitivity.max_false_mark_rate,
                            cfg.sensitivity.train_args)
    test = (out / "test.jsonl").relative_to(ROOT)
    print("\nNow measure it, where it was not trained. Each can be rerun:\n"
          f"  uv run --group train python -m eval.run_sensitivity --classifier {name} --model-only\n"
          f"  uv run --group train python -m eval.run_sensitivity --classifier {name}\n"
          f"  uv run --group train python -m eval.run_sensitivity --classifier {name} --model-only --dataset {test}\n"
          f"  uv run python -m eval.run_sensitivity --dataset {test}\n"
          f"  uv run python -m eval.run_sensitivity --model qwen2.5:7b --model-only --dataset {test}")
    return 0


def cmd_status(args, cfg) -> int:
    ledger = Ledger(args.run)
    for model in ("answer-select", "guard", "answer"):
        rows = ledger.rounds(model)
        if rows:
            print(f"\n{model}")
            for r in rows:
                met = sum(r.get("met", {}).values()), len(r.get("met", {}))
                print(f"  {r['round']:3}  {r['model_name']:44} score {r['score']:7}  targets {met[0]}/{met[1]}"
                      f"  ${r.get('spent_usd', 0)}")
    spend = ledger.dir / "spend.json"
    print(f"\nFrontier spend: ${json.loads(spend.read_text())['usd'] if spend.exists() else 0:.2f} "
          f"of ${cfg.budget.max_usd:.2f}")
    return 0


def unreviewed(ledger: Ledger, up_to: int | None = None) -> list[str]:
    """Rows the reviewer flagged for a person, in the rounds that trained the model (up to `up_to`), that
    have no person's label yet: promotion waits for them."""
    out = []
    for f in sorted((ledger.dir / "guard").glob("round_*/audit.csv")):
        if up_to is not None and int(f.parent.name.split("_")[1]) > up_to:
            continue
        with f.open(encoding="utf-8") as fh:
            out += [f"{f.parent.name}: {r['text'][:60]}" for r in csv.DictReader(fh) if not r.get("your_label")]
    return out


def cmd_promote(args, cfg) -> int:
    ledger = Ledger(args.run)
    model = args.model
    rows = ledger.rounds(model)
    pick = next((r for r in rows if r["round"] == args.round), None) if args.round else ledger.best(model)
    if pick is None:
        print(f"No {model} round to promote in run {args.run}.")
        return 1
    if not all(pick["met"].values()):
        print(f"{pick['model_name']} did not meet every target on the gate sets: {pick['met']}. Not promoted.")
        return 1
    if model == "guard":
        pending = unreviewed(ledger, pick["round"])
        if pending:
            print(f"{len(pending)} rows the reviewer flagged are waiting for you (modelops/runs/{args.run}/guard/"
                  "round_*/audit.csv, column your_label; `guard review` first sends any older ones to the reviewer). "
                  "First few:")
            print("\n".join("  " + p for p in pending[:5]))
            return 1
        name = pick["model_name"]
        print(f"Sealed checks for {name}, each run once (the scripts refuse a second run):\n"
              f"  uv run --group train python -m eval.run_guard_test --set v5 --model {name}\n"
              f"  uv run --group train python -m eval.run_crisis_benchmark --benchmark v2 --model {name}\n"
              "Benchmark v2 was never used by the loop, so it is still an independent test of this model. "
              "Its training data was written by a frontier model; the frozen test sets were drafted with Claude, "
              "so report v2 (written by other people) as the primary result.\n"
              f"If both hold, set guard_model: {name} in the profiles' classifier block and record it in an ADR.")
        return 0
    from modelops.answer_eval import score_frozen_tool_use
    out = score_frozen_tool_use(pick["model_name"])
    print(json.dumps(out["summary"], indent=1))
    print(f"Saved {out['path']}. If it holds, point the profiles' local model at {pick['model_name']} and run the "
          "red-team set once (eval/run_redteam.py) before using it with staff.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modelops", description="Continuous training for the guard "
                                                                        "classifier and the answering model.")
    ap.add_argument("--config", type=Path, default=config.DEFAULT)
    sub = ap.add_subparsers(dest="command", required=True)
    run_id = date.today().strftime("%Y%m%d")

    g = sub.add_parser("guard", help="the guard classifier's loop")
    g.add_argument("action", choices=["run", "review", "compare"],
                   help="run: rounds until a stop condition; review: send rows waiting in audit.csv to the reviewer; "
                        "compare: score guards on the run's gate sets (default: the profiles' guard and every round)")
    g.add_argument("--model", action="append", help="compare: a model folder name in models/ (repeat for several)")
    g.add_argument("--run", default=run_id)
    g.add_argument("--rounds", type=int)
    a = sub.add_parser("answer", help="the answering model's loop")
    a.add_argument("action", choices=["select", "run"])
    a.add_argument("--run", default=run_id)
    a.add_argument("--rounds", type=int)
    a.add_argument("--base", help="an Ollama tag from answer.candidates (default: the best from select)")
    c = sub.add_parser("sensitivity", help="the sensitive-category classifier (ADR 038 step 3)")
    c.add_argument("action", choices=["data", "train"],
                   help="data: frontier-written, blind-labelled text and a test set; train: fine-tune on it")
    c.add_argument("--run", default=run_id)
    s = sub.add_parser("status", help="every round of a run")
    s.add_argument("--run", default=run_id)
    p = sub.add_parser("promote", help="sealed checks for a loop model, after a person's review")
    p.add_argument("model", choices=["guard", "answer"])
    p.add_argument("--run", default=run_id)
    p.add_argument("--round", type=int, help="default: the best-scoring round")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"modelops/loop.yaml: {e}")
        return 2
    from modelops.frontier import FrontierError
    try:
        return {"guard": cmd_guard, "answer": cmd_answer, "status": cmd_status, "promote": cmd_promote,
                "sensitivity": cmd_sensitivity}[args.command](args, cfg)
    except FrontierError as e:
        hint = {"authentication_error": "the key was refused: check the variable named by api_key_env in loop.yaml",
                "permission_error": "the key may not use this model or workspace",
                "not_found_error": "the model name in loop.yaml (frontier.name) is not available to this key",
                "credit balance": "add credits in the Claude Console (Billing)"}
        why = next((h for k, h in hint.items() if k in str(e)), "")
        print(f"Stopped: {e}" + (f"\n  {why}" if why else "") +
              "\nNothing from the unfinished round was recorded; run the same command again to continue.")
        return 3


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["RUNS", "main", "unreviewed"]
