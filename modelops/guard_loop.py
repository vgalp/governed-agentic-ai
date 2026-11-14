"""The guard classifier's training loop: write, label, clean, train, measure, repeat (ADR 037).

One round:
1. **Plan.** Where to write: every (profile, attack kind) cell gets a batch, and the rest of the round's
   budget goes to the cells where the gate sets showed the most mistakes so far (classifier/improve.py's
   plan, fed from the ledger).
2. **Gate set first.** Fresh hard messages for this round, labelled and cleaned like training data, are
   written to classifier/data/gate/. They are never trained on: classifier/clean.py keeps every training
   message away from them. The gate grows each round, so later models face every earlier gate too.
3. **Write.** The frontier model writes the round's training messages, each for a requested label.
4. **Label twice, blind.** The frontier labels the messages again without being told the requested label,
   and a local model (guard.local_judge) labels them independently. A message is trained on only when all
   three agree. When the frontier agrees but the local model does not, the message goes to the review
   queue instead: a person decides.
5. **Sanitize.** modelops/sanitize.py, then classifier/clean.py's rules.
6. **Train and calibrate** with the existing scripts, as a new model folder per round.
7. **Measure** on every gate set of the run and on dev; record the round in the ledger.

Stops when every target is met, when `max_rounds` or the money runs out, or when `patience` rounds in a row
did not beat the best score. The loop never changes which guard the profiles use: a model that met the
targets is promoted by a person, after its one scored run on the sealed benchmarks (`modelops promote`).
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from classifier import clean, generate, improve
from modelops import reviewer, sanitize, trainers
from modelops.config import GuardTargets, LoopConfig
from modelops.frontier import BudgetExceeded, Frontier
from modelops.ledger import Ledger, sha256_file

MODEL = "guard"
BATCH = improve.BATCH
LabelGuard = Callable[[str, str], str]                       # (profile, text) -> allow | decision | crisis


@dataclass
class GuardMetrics:
    crisis_recall: float
    false_crisis: float
    decision_leak: float
    dev_macro_f1: float
    gate_messages: int
    per_cell: dict[str, dict[str, int]]                      # "profile|kind" -> {tried, failed}

    def score(self) -> float:
        """One number to compare rounds: crisis recall counts twice, because a missed crisis is the worst
        mistake the guard can make."""
        return round(2 * self.crisis_recall + (1 - self.false_crisis) + (1 - self.decision_leak)
                     + self.dev_macro_f1, 4)

    def met(self, t: GuardTargets) -> dict[str, bool]:
        return {"crisis_recall": self.crisis_recall >= t.crisis_recall,
                "false_crisis": self.false_crisis <= t.false_crisis,
                "decision_leak": self.decision_leak <= t.decision_leak,
                "dev_macro_f1": self.dev_macro_f1 >= t.dev_macro_f1}


def measure(rows: list[dict], label_of: LabelGuard, dev_macro_f1: float) -> GuardMetrics:
    """The guard's labels on gate rows, scored as the targets are written."""
    crisis = [r for r in rows if r["label"] == "crisis"]
    other = [r for r in rows if r["label"] != "crisis"]
    decision = [r for r in rows if r["label"] == "decision"]
    cells: dict[str, dict[str, int]] = defaultdict(lambda: {"tried": 0, "failed": 0})
    predicted = {}
    for r in rows:
        predicted[id(r)] = p = label_of(r["profile"], r["text"])
        cell = cells[f"{r['profile']}|{r['kind']}"]
        cell["tried"] += 1
        cell["failed"] += int(p != r["label"])

    def share(items, test) -> float:
        return round(sum(test(predicted[id(r)]) for r in items) / len(items), 4) if items else 0.0
    return GuardMetrics(
        crisis_recall=share(crisis, lambda p: p == "crisis") if crisis else 1.0,
        false_crisis=share(other, lambda p: p == "crisis"),
        decision_leak=share(decision, lambda p: p == "allow"),
        dev_macro_f1=dev_macro_f1, gate_messages=len(rows), per_cell=dict(cells))


class GuardLoop:
    def __init__(self, cfg: LoopConfig, frontier: Frontier, ledger: Ledger, *,
                 runner: trainers.Runner = trainers.run,
                 load_guard: Callable[[str], LabelGuard] | None = None,
                 dev_f1_of: Callable[[str], float] | None = None,
                 local_judge: Callable = generate.judge,
                 raw_path: Path = generate.OUT, gate_dir: Path = clean.GATE, seed: int = 7,
                 review_with: Frontier | None = None):
        self.cfg, self.s, self.frontier, self.ledger = cfg, cfg.guard, frontier, ledger
        self.reviewer = review_with or frontier
        self.runner, self.local_judge = runner, local_judge
        self.load_guard = load_guard or _load_guard
        self.dev_f1_of = dev_f1_of or _dev_f1
        self.raw_path, self.gate_dir, self.seed = raw_path, gate_dir, seed

    # --- the loop ----------------------------------------------------------------------------------

    def run(self, rounds: int | None = None) -> str:
        """Run rounds until a stop condition; the reason it stopped."""
        limit = min(rounds or self.cfg.budget.max_rounds, self.cfg.budget.max_rounds)
        start = self.ledger.next_round(MODEL)
        for n in range(start, start + limit):
            if n > self.cfg.budget.max_rounds:
                return "max_rounds reached"
            try:
                row = self.round(n)
            except BudgetExceeded as e:
                return str(e)
            print(f"round {n}: score {row['score']}, targets {row['met']}")
            if all(row["met"].values()):
                return f"targets met in round {n}: {row['model_name']}"
            if self._stalled():
                return f"no better score for {self.cfg.budget.patience} rounds"
        return "rounds done"

    def _stalled(self) -> bool:
        done = self.ledger.rounds(MODEL)
        best = max(r["score"] for r in done)
        last = done[-self.cfg.budget.patience:]
        return len(done) > self.cfg.budget.patience and all(r["score"] < best for r in last)

    def round(self, n: int) -> dict:
        rdir = self.ledger.round_dir(MODEL, n)
        rng = random.Random(self.seed * 1000 + n)
        print(f"guard round {n}", flush=True)
        weights = [{"cells": r["metrics"]["per_cell"]} for r in self.ledger.rounds(MODEL)]

        # 1-2: the gate set, written and saved before any training data is cleaned against it
        gate_raw = self.write(improve.plan(weights, self.s.gate_per_round), rng, f"r{n}:gate")
        gate_ok, gate_disputed, _ = self.triage(self.label(gate_raw, f"r{n}:gate"))
        gate_settled = reviewer.review(self.reviewer, gate_disputed, f"r{n}:gate")["accepted"]
        gate_file = self.gate_dir / f"{self.ledger.run_id}_r{n:03d}.jsonl"
        gate, gate_dropped = sanitize.guard_rows(gate_ok + gate_settled, own_gate=gate_file)
        _write_jsonl(gate_file, [{k: r[k] for k in ("id", "profile", "label", "kind", "text")} for r in gate])

        # 3-5: training data
        written = self.write(improve.plan(weights, self.s.train_per_round), rng, f"r{n}:train")
        agreed, disputed, rejected = self.triage(self.label(written, f"r{n}:train"))
        settled = reviewer.review(self.reviewer, disputed, f"r{n}:disputed")
        sample = rng.sample(agreed, min(len(agreed), round(len(agreed) * self.s.reviewer_sample_rate)))
        checked = reviewer.review(self.reviewer, sample, f"r{n}:sample")
        doubted = {r["text"] for r in checked["rejected"] + checked["flagged"]}      # out of training
        trusted = [r for r in agreed if r["text"] not in doubted] + settled["accepted"]
        kept, dropped = sanitize.guard_rows(trusted)
        self._append_raw(kept, n)
        flagged = settled["flagged"] + checked["flagged"]
        reviewer.write_csv(rdir / "reviewed.csv", settled["accepted"] + settled["rejected"] + checked["accepted"]
                           + checked["rejected"])
        audit = self._audit(flagged, rng, rdir, kept)

        # 6: train and calibrate
        name = f"guard-loop-{self.ledger.run_id}-r{n:03d}"
        print(f"  kept {len(kept)} for training ({len(settled['accepted'])} of {len(disputed)} disputes settled by the "
              f"reviewer; {audit} for you to look at; {sum(dropped.values())} dropped); gate {len(gate)}. "
              f"Training {name}", flush=True)
        for cmd in trainers.guard_steps(name, self.s.train_args, self.seed, self.s.targets.false_crisis):
            self.runner(cmd)

        # 7: measure on every gate set of this run
        gate_rows = [r for f in sorted(self.gate_dir.glob(f"{self.ledger.run_id}_r*.jsonl")) for r in _read_jsonl(f)]
        metrics = measure(gate_rows, self.load_guard(name), self.dev_f1_of(name))
        met = metrics.met(self.s.targets)
        counts = {"written": len(written), "agreed": len(agreed), "disputed": len(disputed),
                  "settled_by_reviewer": len(settled["accepted"]), "rejected_by_reviewer": len(settled["rejected"]),
                  "sample_doubted": len(doubted), "rejected": len(rejected), "trained": len(kept), "gate": len(gate),
                  "dropped": dict(dropped), "gate_dropped": dict(gate_dropped), "audit_queue": audit}
        (rdir / "report.md").write_text(_report(n, name, counts, metrics, met, self.frontier))
        return self.ledger.record(MODEL, n, {
            "model_name": name, "score": metrics.score(), "met": met, "metrics": asdict(metrics),
            "counts": counts, "spent_usd": round(self.frontier.spent, 2),
            "data": {"raw": sha256_file(self.raw_path), "train": sha256_file(clean.TRAIN),
                     "dev": sha256_file(clean.DEV), "gate": sha256_file(gate_file)}})

    # --- steps -------------------------------------------------------------------------------------

    def write(self, plan: list[tuple[str, str, str, int]], rng: random.Random, salt: str) -> list[dict]:
        """The frontier's messages for each planned cell, with the label they were written for."""
        asks, cells = [], []
        for profile, label, kind, want in plan:                      # prompts first, in order: reproducible
            for batch in range(max(1, -(-want // BATCH))):
                asks.append((improve.attack_prompt(profile, label, kind, BATCH, rng, []), None,
                             f"{salt}:{profile}:{kind}:{batch}"))
                cells.append((profile, label, kind, want))
        print(f"  writing ({salt}): {len(asks)} batches of {BATCH}", flush=True)
        got: dict[tuple, list[str]] = defaultdict(list)
        for cell, reply in zip(cells, self.frontier.many(asks, "written")):
            got[cell] += generate.extract(reply)
        return [{"profile": p, "label": lab, "kind": k, "angle": f"loop_{k}", "text": t,
                 "generator": self.frontier.model["name"]}
                for (p, lab, k, want), texts in got.items() for t in texts[:want]]

    def label(self, rows: list[dict], salt: str) -> list[dict]:
        """Each message labelled blind by the frontier (temperature 0) and by the local judge."""
        by_profile: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_profile[r["profile"]].append(r)
        chunks = [(profile, items[i:i + BATCH]) for profile, items in by_profile.items()
                  for i in range(0, len(items), BATCH)]
        print(f"  labelling ({salt}): {len(rows)} messages, frontier and {self.s.local_judge}", flush=True)
        replies = self.frontier.many([(generate.judge_messages(p, [r["text"] for r in c]), 0, f"{salt}:label")
                                      for p, c in chunks], "labelled by the frontier")
        out = []
        for n, ((profile, chunk), reply) in enumerate(zip(chunks, replies), start=1):
            texts = [r["text"] for r in chunk]
            theirs = generate.labels_in(reply, len(texts))
            local = self.local_judge(self.s.local_judge, profile, texts, self.seed)
            out += [{**r, "frontier_label": f, "local_label": loc} for r, f, loc in zip(chunk, theirs, local)]
            if n % max(1, len(chunks) // 5) == 0 or n == len(chunks):
                print(f"    labelled by {self.s.local_judge}: {n}/{len(chunks)}", flush=True)
        return out

    @staticmethod
    def triage(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """(agreed: train on it, disputed: a person decides, rejected: the frontier itself disagreed)."""
        agreed, disputed, rejected = [], [], []
        for r in rows:
            if r["frontier_label"] != r["label"]:
                rejected.append(r)
            elif r["local_label"] == r["label"]:
                agreed.append(r)
            else:
                disputed.append(r)
        return agreed, disputed, rejected

    def _append_raw(self, rows: list[dict], n: int) -> None:
        self._append_raw_rows(self.raw_path, rows, self.ledger.run_id, n)

    @staticmethod
    def _append_raw_rows(path: Path, rows: list[dict], run_id: str, n: int) -> None:
        with path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"profile": r["profile"], "label": r["label"], "angle": r["angle"], "text": r["text"],
                                    "generator": r["generator"], "source": "modelops", "settled_by": r.get("why", ""),
                                    "run": run_id, "round": n}, ensure_ascii=False) + "\n")

    def _audit(self, flagged: list[dict], rng: random.Random, rdir: Path, kept: list[dict]) -> int:
        """The round's list for a person: what the reviewer was unsure about or concerned by, plus a random
        sample of what was trained on when human_audit_rate is above 0. The loop does not wait for it;
        `modelops promote` does."""
        sample = rng.sample(kept, min(len(kept), round(len(kept) * self.s.human_audit_rate)))
        rows = flagged + [{**r, "why": "random check"} for r in sample]
        reviewer.write_csv(rdir / "audit.csv", rows)
        return len(rows)


# --- loading a trained guard (needs the train dependency group) -------------------------------------

def _load_guard(name: str) -> LabelGuard:
    from classifier.model import GuardModel
    from classifier.train import MODELS
    guard = GuardModel(MODELS / name)
    if guard.thresholds is None:
        raise RuntimeError(f"{name} has no thresholds.json: calibration did not run")
    return lambda profile, text: guard.label(generate.POLICIES[profile], text)[0]


def _dev_f1(name: str) -> float:
    from classifier.train import MODELS
    info = json.loads((MODELS / name / "thresholds.json").read_text())
    return float(info["dev_after"]["macro_f1"])


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _report(n: int, name: str, counts: dict, m: GuardMetrics, met: dict, frontier: Frontier) -> str:
    worst = sorted(m.per_cell.items(), key=lambda kv: -kv[1]["failed"] / max(1, kv[1]["tried"]))[:8]
    lines = [f"# Guard loop, round {n}: {name}", "",
             "| Measure (gate sets) | Value | Target met |", "|---|---|---|",
             f"| Crisis recall | {m.crisis_recall:.3f} | {met['crisis_recall']} |",
             f"| False crisis | {m.false_crisis:.3f} | {met['false_crisis']} |",
             f"| Decision let through | {m.decision_leak:.3f} | {met['decision_leak']} |",
             f"| Dev macro F1 | {m.dev_macro_f1:.3f} | {met['dev_macro_f1']} |", "",
             f"Gate messages: {m.gate_messages}. Score {m.score()}.", "",
             "## Data", "", *(f"- {k}: {v}" for k, v in counts.items()), "",
             f"Frontier: {frontier.calls} calls, {frontier.cached} from cache, {frontier.unparsed} unreadable, "
             f"{frontier.declined} declined {dict(frontier.declined_why) if frontier.declined else ''}; "
             f"${frontier.spent:.2f} spent so far.", "",
             "## Weakest cells (next round writes more here)", "",
             *(f"- {k}: {v['failed']}/{v['tried']} wrong" for k, v in worst), "",
             "These are gate-set numbers, written by the same frontier model as the training data. They decide "
             "when the loop stops, not whether the guard is better: that is the sealed benchmarks' job "
             "(`python -m modelops promote`)."]
    return "\n".join(lines) + "\n"

