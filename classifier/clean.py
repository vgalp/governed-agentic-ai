"""Clean the generated messages, keep them away from the test set, and split them.

    uv run python -m classifier.clean                       # raw.jsonl -> train/dev + review.csv
    uv run python -m classifier.clean --apply-review        # after you filled in review.csv

1. Drop duplicates (same text after normalizing case, spaces and punctuation).
2. Drop anything too close to a frozen test message or a red-team prompt
   (eval/datasets/*.jsonl): similarity >= 0.75. The test set must stay unseen.
3. Drop any message containing a name used in the test set.
4. Split into train (90%) and dev (10%), stratified by profile and label. Dev is for
   choosing thresholds and stopping training; the test set is never used for that.
5. Write review.csv: a random sample per profile and label for you to check by hand, plus
   every crisis message rescued from rejected.jsonl (see RISK_WORDS below).
   Review it with: uv run python -m classifier.review (one message at a time).

--apply-review applies your corrections to train/dev and prints how often the generator's
label was right (it goes in the data card).
"""

import argparse
import csv
import difflib
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path

from classifier.generate import TEST_SET, test_names

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW, TRAIN, DEV, REVIEW = DATA / "raw.jsonl", DATA / "train.jsonl", DATA / "dev.jsonl", DATA / "review.csv"
REJECTED = DATA / "rejected.jsonl"
HELD_OUT = sorted((ROOT.parent / "eval" / "datasets").glob("*.jsonl"))   # test set + red-team sets
LABELS = ("allow", "decision", "crisis")
TOO_CLOSE = 0.75
REVIEW_PER_CELL = 20

# The second look in generate.py is a small model, and it tends to set aside the hardest crisis
# messages: an overdose question looks like a dose question to it, an indirect warning sign looks
# like ordinary sadness. Crisis messages it set aside that contain an explicit risk word are given
# back ("rescued"), and EVERY rescued message goes to review.csv for a person to confirm.
RISK_WORDS = re.compile(
    r"(suicid|kill (him|her|my|them|your)sel|end (it|my life|his life|her life|things)|lethal|letal|deadly|"
    r"fatal|mortel|mortal|tuer|matar|morir|mourir|\bdie\b|dying|\bdead\b|overdos|never wake|not wake|"
    r"wake up again|burden|un poids|una carga|better off (dead|gone|without)|jump|bridge|wrist|rope|"
    r"\bgun\b|knife|cutting|hurt (him|her|my|them)sel)", re.I)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúñüàâçèêëîïôûœ ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def _words(text: str) -> set[str]:
    return set(text.split())


def held_out() -> list[str]:
    out = []
    for path in HELD_OUT:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(norm(json.loads(line)["text"]))
    return out


def too_close(text: str, others: list[tuple[str, set[str]]]) -> float:
    """Highest similarity to any held-out message (word overlap first, so it stays fast)."""
    words, best = _words(text), 0.0
    for other, other_words in others:
        if len(words & other_words) * 3 < min(len(words), len(other_words)):
            continue
        best = max(best, difflib.SequenceMatcher(None, text, other).ratio())
    return best


def clean(rows: list[dict], held: list[str], names: set[str]) -> tuple[list[dict], Counter]:
    others = [(h, _words(h)) for h in held]
    seen, kept, dropped = set(), [], Counter()
    for r in rows:
        key = norm(r["text"])
        if r.get("label") not in LABELS:
            dropped["bad label"] += 1
        elif key in seen:
            dropped["duplicate"] += 1
        elif any(n in r["text"] for n in names):
            dropped["test-set name"] += 1
        elif too_close(key, others) >= TOO_CLOSE:
            dropped["too close to a held-out message"] += 1
        else:
            seen.add(key)
            kept.append({**r, "id": "g-" + hashlib.sha1(key.encode()).hexdigest()[:10]})
    return kept, dropped


def rescue(rejected: list[dict]) -> list[dict]:
    """Crisis messages the second look set aside that still contain an explicit risk word."""
    out = []
    for r in rejected:
        if r.get("label") == "crisis" and RISK_WORDS.search(r["text"]):
            out.append({k: v for k, v in r.items() if k != "judge"} | {"rescued": True})
    return out


def split(rows: list[dict], seed: int) -> tuple[list[dict], list[dict]]:
    rng, train, dev = random.Random(seed), [], []
    cells: dict[tuple, list] = {}
    for r in rows:
        cells.setdefault((r["profile"], r["label"]), []).append(r)
    for cell in cells.values():
        rng.shuffle(cell)
        n_dev = max(1, len(cell) // 10)
        dev += cell[:n_dev]
        train += cell[n_dev:]
    return train, dev


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_review(rows: list[dict], seed: int) -> None:
    """A random sample per profile and label, plus every rescued message."""
    rng, sample = random.Random(seed + 1), [r for r in rows if r.get("rescued")]
    cells: dict[tuple, list] = {}
    for r in rows:
        if not r.get("rescued"):
            cells.setdefault((r["profile"], r["label"]), []).append(r)
    for key in sorted(cells):
        sample += rng.sample(cells[key], min(REVIEW_PER_CELL, len(cells[key])))
    REVIEW.with_name("review_progress.json").unlink(missing_ok=True)   # a new sample starts a new review
    with REVIEW.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "profile", "angle", "label", "text", "your_label", "note"])
        for r in sample:
            angle = r["angle"] + (" (rescued)" if r.get("rescued") else "")
            w.writerow([r["id"], r["profile"], angle, r["label"], r["text"], "", ""])


def apply_review() -> None:
    from classifier.review import finished
    if not finished():
        raise SystemExit("The review is not finished. Run: uv run python -m classifier.review")
    with REVIEW.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fixes = {r["id"]: r["your_label"].strip().lower() for r in rows if r["your_label"].strip()}
    bad = {v for v in fixes.values() if v not in (*LABELS, "drop")}
    if bad:
        raise SystemExit(f"review.csv: your_label must be allow, decision, crisis or drop, not {sorted(bad)}")
    given = {r["id"]: r["label"] for r in rows}
    changed = {k: v for k, v in fixes.items() if v != given.get(k)}
    for path in (TRAIN, DEV):
        out = []
        for r in read_jsonl(path):
            fix = changed.get(r["id"])
            if fix == "drop":
                continue
            if fix:
                r = {**r, "label": fix, "checked": "corrected"}
            elif r["id"] in given:
                r = {**r, "checked": "confirmed"}
            out.append(r)
        write_jsonl(path, out)
    right = len(rows) - len(changed)
    print(f"Reviewed {len(rows)}: generator label right for {right} ({right / len(rows):.0%}), "
          f"corrected {sum(v != 'drop' for v in changed.values())}, dropped {sum(v == 'drop' for v in changed.values())}")
    by = Counter((r["label"], changed.get(r["id"], r["label"])) for r in rows)
    for (was, now), n in sorted(by.items()):
        if was != now:
            print(f"  {was:8} -> {now:8} {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply-review", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if args.apply_review:
        apply_review()
        return
    rescued = rescue(read_jsonl(REJECTED)) if REJECTED.exists() else []
    rows, dropped = clean(read_jsonl(RAW) + rescued, held_out(), test_names())
    train, dev = split(rows, args.seed)
    write_jsonl(TRAIN, train)
    write_jsonl(DEV, dev)
    write_review(train + dev, args.seed)
    print(f"Kept {len(rows)}: train {len(train)}, dev {len(dev)}. Dropped: {dict(dropped) or 'none'}")
    print(f"Rescued from rejected.jsonl: {sum(1 for r in rows if r.get('rescued'))} crisis messages "
          f"(all of them are in review.csv)")
    for profile in sorted({r['profile'] for r in rows}):
        c = Counter(r["label"] for r in rows if r["profile"] == profile)
        print(f"  {profile:17} " + "  ".join(f"{k} {c[k]}" for k in LABELS))
    print("Review the sample: uv run python -m classifier.review, then: uv run python -m classifier.clean --apply-review")


if __name__ == "__main__":
    assert TEST_SET.exists()
    main()