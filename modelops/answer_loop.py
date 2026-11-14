"""The answering model's loop: choose a base model, then write, train, measure, repeat (ADR 037).

`select` scores every candidate in loop.yaml (as served by Ollama, unchanged) on one selection gate set,
and records each in the ledger. The best becomes the base for training.

One training round:
1. **Gate first.** Fresh routing questions and answer examples, checked like training data, saved in the
   run folder and never trained on; later training examples are kept away from them (and from every
   frozen evaluation set). The gate grows each round.
2. **Write.** Routing and answer examples, more of the kinds the base model got wrong on the last gate.
3. **Check.** modelops/answer_data.py: only valid citations, no medication advice, no contact details,
   known destinations. Failed examples are counted by reason, never repaired by hand.
4. **Train.** A LoRA adapter on the base weights, from all examples accepted so far (each round trains
   from the base again, so a round never stacks on a worse one), merged and imported into Ollama as
   `gaai-answer:<run>-rNNN`.
5. **Measure** on every gate set of the run; record the round.

Stops as the guard loop does: targets met, rounds or money spent, or no progress for `patience` rounds.
The profiles keep their model until a person changes it, after the sealed checks (`modelops promote`).
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from classifier import clean as guard_clean
from llm import providers
from modelops import answer_data as data
from modelops import sanitize, trainers
from modelops.answer_eval import AnswerMetrics, Ask, evaluate
from modelops.config import LoopConfig
from modelops.frontier import BudgetExceeded, Frontier, parse_json
from modelops.ledger import Ledger
from profiles.loader import Profile, load_profile

MODEL, SELECT = "answer", "answer-select"
BATCH = 8


def ollama_ask(tag: str) -> Ask:
    model = {"provider": "ollama", "name": tag, "location": "local", "temperature": 0, "max_tokens": 800}
    return lambda messages: providers.complete(model, messages)[0]


class AnswerLoop:
    def __init__(self, cfg: LoopConfig, frontier: Frontier, ledger: Ledger, *,
                 runner: trainers.Runner = trainers.run, ask_model: Callable[[str], Ask] = ollama_ask,
                 profiles: dict[str, Profile] | None = None, seed: int = 7):
        self.cfg, self.s, self.frontier, self.ledger = cfg, cfg.answer, frontier, ledger
        self.runner, self.ask_model, self.seed = runner, ask_model, seed
        self.profiles = profiles or {name: load_profile(name) for name in self.s.profiles}
        self.dests = {name: data.destinations(p) for name, p in self.profiles.items()}
        self.home = ledger.dir / MODEL
        self.home.mkdir(parents=True, exist_ok=True)

    # --- choosing the base ---------------------------------------------------------------------------

    def select(self) -> str:
        gate = self._gate("select", self.s.gate_per_round)
        grade = self._grader()
        best: tuple[float, str] | None = None
        done = {r["model_name"] for r in self.ledger.rounds(SELECT)}
        for i, tag in enumerate(self.s.candidates, start=1):
            if tag in done:
                continue
            m = evaluate(gate, self.profiles, self.ask_model(tag), grade)
            self.ledger.record(SELECT, i, {"model_name": tag, "score": m.score(), "metrics": asdict(m),
                                           "met": m.met(self.s.targets, self.s.judge)})
            print(f"{tag:24} score {m.score()}  routing {m.tool_choice:.2f}  paraphrase {m.paraphrase:.2f}  "
                  f"citations {m.citations_valid:.2f}  no dose {m.no_dose:.2f}  declines {m.declines_correct:.2f}")
        for r in self.ledger.rounds(SELECT):
            if best is None or r["score"] > best[0]:
                best = (r["score"], r["model_name"])
        assert best is not None
        return best[1]

    # --- training rounds -------------------------------------------------------------------------------

    def run(self, rounds: int | None = None, base: str | None = None) -> str:
        base = base or (self.ledger.best(SELECT) or {}).get("model_name") or self.select()
        if base not in self.s.base_weights:
            return f"no base_weights for {base} in loop.yaml: add its Hugging Face id to train it"
        limit = min(rounds or self.cfg.budget.max_rounds, self.cfg.budget.max_rounds)
        start = self.ledger.next_round(MODEL)
        for n in range(start, start + limit):
            if n > self.cfg.budget.max_rounds:
                return "max_rounds reached"
            try:
                row = self.round(n, base)
            except BudgetExceeded as e:
                return str(e)
            print(f"round {n}: score {row['score']}, targets {row['met']}")
            if all(row["met"].values()):
                return f"targets met in round {n}: {row['model_name']}"
            done = self.ledger.rounds(MODEL)
            best = max(r["score"] for r in done)
            if len(done) > self.cfg.budget.patience and all(
                    r["score"] < best for r in done[-self.cfg.budget.patience:]):
                return f"no better score for {self.cfg.budget.patience} rounds"
        return "rounds done"

    def round(self, n: int, base: str) -> dict:
        rdir = self.ledger.round_dir(MODEL, n)
        self._gate(f"r{n:03d}", self.s.gate_per_round)
        weak = self._weak_kinds()
        rows, dropped = self.write(self.s.train_per_round, weak, f"r{n}:train")
        pool = self._append_examples(rows)
        data_dir = rdir / "data"
        _split_write(pool, data_dir, random.Random(self.seed + n))

        tag = f"gaai-answer:{self.ledger.run_id}-r{n:03d}".lower()
        for cmd in trainers.lora_steps(self.s.backend, self.s.base_weights[base], data_dir, rdir / "model", self.s.lora):
            self.runner(cmd)
        modelfile, cmd = trainers.ollama_import(tag, rdir / "model" / "merged")
        (rdir / "model").mkdir(parents=True, exist_ok=True)
        (rdir / "model" / "Modelfile").write_text(modelfile)
        self.runner(cmd)

        m = evaluate(self._all_gates(), self.profiles, self.ask_model(tag), self._grader())
        met = m.met(self.s.targets, self.s.judge)
        counts = {"written": len(rows) + sum(dropped.values()), "kept": len(rows), "pool": len(pool),
                  "dropped": dict(dropped), "base": base}
        (rdir / "report.md").write_text(_report(n, tag, counts, m, met, self.frontier))
        return self.ledger.record(MODEL, n, {"model_name": tag, "score": m.score(), "met": met,
                                             "metrics": asdict(m), "counts": counts,
                                             "spent_usd": round(self.frontier.spent, 2)})

    # --- writing examples ------------------------------------------------------------------------------

    def write(self, total: int, weights: dict[str, float], salt: str,
              held: list[tuple[str, set[str]]] | None = None) -> tuple[list[dict], Counter]:
        """Checked training rows (chat format, tagged with task and kind), and why others were dropped."""
        rng = random.Random(f"{self.seed}:{salt}")
        held = held if held is not None else self._held()
        cells = [("route", k, p) for k in data.ROUTE_KINDS for p in self.profiles] + \
                [("answer", k, p) for k in data.ANSWER_KINDS for p in self.profiles]
        share = {c: 1.0 + weights.get(c[1], 0.0) for c in cells}
        scale = total / sum(share.values())
        rows, dropped, seen = [], Counter(), set()
        for task, kind, pname in cells:
            want = max(1, round(share[(task, kind, pname)] * scale))
            for item in self._items(task, kind, pname, want, rng, salt):
                problems = (data.route_problems(item, self.dests[pname]) if task == "route"
                            else data.answer_problems(item, kind, self.profiles[pname]))
                key = guard_clean.norm(item.get("question") or "")
                if not problems and key in seen:
                    problems = ["duplicate"]
                if not problems and sanitize.too_close(item["question"], held):
                    problems = ["too close to a gate or evaluation question"]
                if problems:
                    dropped.update(problems)
                    continue
                seen.add(key)
                row = data.route_row(self.dests[pname], item) if task == "route" else data.answer_row(
                    self.profiles[pname], item)
                case = {k: item[k] for k in ("question", "path", "notes", "answer") if k in item}
                rows.append({**row, "task": task, "kind": kind, "profile": pname, "case": case})
        return rows, dropped

    def _items(self, task: str, kind: str, pname: str, want: int, rng: random.Random, salt: str) -> list[dict]:
        p = self.profiles[pname]
        asks = [((data.route_request(p, self.dests[pname], kind, BATCH, rng) if task == "route"
                  else data.answer_request(p, kind, BATCH, rng)), None, f"{salt}:{task}:{kind}:{pname}:{batch}")
                for batch in range(max(1, -(-want // BATCH)))]
        out: list[dict] = []
        for reply in self.frontier.many(asks, f"{task} {kind} {pname}"):
            obj = parse_json(reply)
            items = obj.get("items") if obj else None
            out += [{**i, "kind": kind, "profile": pname} for i in items or [] if isinstance(i, dict)]
        return out[:want]

    # --- gate sets -------------------------------------------------------------------------------------

    def _gate(self, name: str, total: int) -> dict:
        """A held-out set: routing questions and answer cases, written once and kept in the run folder."""
        path = self.home / f"gate_{name}.json"
        if path.exists():
            return json.loads(path.read_text())
        rows, _ = self.write(total, {}, f"gate:{name}", held=sanitize.prepare_held(sanitize.held_out_texts()))
        gate = {"routing": [], "answers": []}
        for r in rows:
            gate["routing" if r["task"] == "route" else "answers"].append(
                {"profile": r["profile"], "kind": r["kind"], **r["case"]})
        path.write_text(json.dumps(gate, ensure_ascii=False, indent=1))
        return gate

    def _all_gates(self) -> dict:
        out = {"routing": [], "answers": []}
        for f in sorted(self.home.glob("gate_r*.json")):
            g = json.loads(f.read_text())
            out["routing"] += g["routing"]
            out["answers"] += g["answers"]
        return out

    def _held(self) -> list[tuple[str, set[str]]]:
        texts = sanitize.held_out_texts()
        for f in self.home.glob("gate_*.json"):
            g = json.loads(f.read_text())
            texts += [guard_clean.norm(c["question"]) for c in g["routing"] + g["answers"]]
        return sanitize.prepare_held(texts)

    def _weak_kinds(self) -> dict[str, float]:
        """Error rate per kind on the last measured gate (routing kinds; answer checks by their kind)."""
        last = (self.ledger.rounds(MODEL) or self.ledger.rounds(SELECT) or [None])[-1]
        if not last:
            return {}
        m = last["metrics"]
        weak = {k: 1 - v for k, v in (m.get("by_kind") or {}).items()}
        weak["grounded"] = 1 - m.get("citations_valid", 1)
        weak["decline"] = 1 - m.get("declines_correct", 1)
        weak["quote_dose"] = 1 - m.get("no_dose", 1)
        return {k: round(3 * v, 3) for k, v in weak.items()}

    def _append_examples(self, rows: list[dict]) -> list[dict]:
        pool = self.home / "examples.jsonl"
        with pool.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return [json.loads(line) for line in pool.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _grader(self):
        if not self.s.judge:
            return None
        return lambda messages: self.frontier.json(messages, temperature=0, salt="grade")


def _split_write(rows: list[dict], folder: Path, rng: random.Random) -> None:
    """train.jsonl and valid.jsonl (90/10) in chat format, as mlx_lm and peft_train.py read them."""
    folder.mkdir(parents=True, exist_ok=True)
    by_cell: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_cell[(r["task"], r["kind"])].append({"messages": r["messages"]})
    train, valid = [], []
    for items in by_cell.values():
        rng.shuffle(items)
        cut = max(1, len(items) // 10) if len(items) > 1 else 0
        valid += items[:cut]
        train += items[cut:]
    for name, part in (("train", train), ("valid", valid)):
        (folder / f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in part))


def _report(n: int, tag: str, counts: dict, m: AnswerMetrics, met: dict, frontier: Frontier) -> str:
    rows = [("Routing, all", m.tool_choice, "tool_choice"), ("Routing, paraphrases", m.paraphrase, "paraphrase"),
            ("Citations only of given notes", m.citations_valid, "citations_valid"),
            ("No medication advice", m.no_dose, "no_dose"), ("Forbidden decisions declined", m.declines_correct,
                                                             "declines_correct")]
    lines = [f"# Answer loop, round {n}: {tag}", "", "| Measure (gate sets) | Value | Target met |", "|---|---|---|",
             *(f"| {name} | {v:.3f} | {met[k]} |" for name, v, k in rows)]
    if m.judge_score is not None:
        lines.append(f"| Frontier grade (1-5) | {m.judge_score:.2f} | {met.get('judge_score')} |")
    lines += ["", f"Score {m.score()} over {m.cases} gate cases. Routing by kind: {m.by_kind}.", "",
              "## Data", "", *(f"- {k}: {v}" for k, v in counts.items()), "",
              f"Frontier: ${frontier.spent:.2f} spent so far.", "",
              "Gate numbers decide when the loop stops. Promotion needs the frozen sets "
              "(`python -m modelops promote answer`)."]
    return "\n".join(lines) + "\n"
