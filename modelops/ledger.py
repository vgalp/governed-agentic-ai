"""What each round of the training loop did, append-only, so a run can be resumed and audited (ADR 037).

    modelops/runs/<run id>/
      ledger.jsonl        one line per finished round: model, data hashes, metrics, targets met, spend
      spend.json          frontier spend so far (modelops/frontier.py)
      frontier_cache/     replies, by request hash
      <model>/round_NNN/  that round's data, gate set, report

A round is written to the ledger only when it has finished, so a crash leaves the ledger at the last
complete round and `resume` starts the next one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"


def sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class Ledger:
    def __init__(self, run_id: str, root: Path = RUNS):
        if not run_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("a run id is letters, digits, - and _ only")
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = self.dir / "ledger.jsonl"

    def rounds(self, model: str) -> list[dict]:
        if not self._file.exists():
            return []
        rows = [json.loads(line) for line in self._file.read_text().splitlines() if line.strip()]
        return [r for r in rows if r["model"] == model]

    def next_round(self, model: str) -> int:
        done = self.rounds(model)
        return done[-1]["round"] + 1 if done else 1

    def round_dir(self, model: str, n: int) -> Path:
        d = self.dir / model / f"round_{n:03d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def record(self, model: str, n: int, entry: dict) -> dict:
        row = {"model": model, "round": n, "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               **entry}
        with self._file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def best(self, model: str) -> dict | None:
        done = [r for r in self.rounds(model) if r.get("score") is not None]
        return max(done, key=lambda r: r["score"]) if done else None
