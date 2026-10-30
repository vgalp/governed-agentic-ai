"""Clean the generated messages, keep them away from the test set, and split them.

    uv run python -m classifier.clean                                     # raw.jsonl -> train/dev + review.csv
    uv run python -m classifier.review                                    # check review.csv
    uv run python -m classifier.clean --apply-review --reviewer "NAME"    # record and apply the review

1. Drop duplicates (same text after normalizing case, spaces and punctuation), and messages
   that are the same as another one apart from the person's name ("same template").
2. Drop anything too close to a frozen test message, a red-team prompt (eval/datasets/*.jsonl)
   or the holdout of confirmed guard mistakes (data/found/holdout.jsonl, classifier/improve.py):
   similarity >= 0.75. They must stay unseen.
3. Drop any message containing a name used in the test set.
4. Apply every review recorded so far (reviewed.csv): corrected labels, dropped messages.
   Reviews are kept there for good, so cleaning again never loses them.
5. Split into train (90%) and dev (10%), stratified by profile and label. Dev is for
   choosing thresholds and stopping training; the test set is never used for that.
6. Write review.csv with what still needs a look: every crisis message rescued from
   rejected.jsonl (see RISK_WORDS below), and REVIEW_PER_CELL random messages from each
   profile, label and angle that has no reviewed message yet. What was reviewed adds nothing.

--apply-review records review.csv in reviewed.csv with who reviewed it (--reviewer, for example
"Claude (claude-opus-5-5); checked by vgalperin"), applies it to train/dev, and prints how often
the generator's label was right (it goes in the data card).
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
REVIEWED = DATA / "reviewed.csv"
HELD_OUT = sorted((ROOT.parent / "eval" / "datasets").glob("*.jsonl"))   # test set + red-team sets
FOUND_HOLDOUT = DATA / "found" / "holdout.jsonl"   # confirmed guard mistakes kept out of training (improve.py)
LABELS = ("allow", "decision", "crisis")
TOO_CLOSE = 0.75
REVIEW_PER_CELL = 20
REVIEW_FIELDS = ["id", "profile", "angle", "label", "text", "your_label", "note"]
REVIEWED_FIELDS = ["id", "profile", "label", "your_label", "reviewer", "note"]

# The second look in generate.py is a small model, and it tends to set aside the hardest crisis
# messages: an overdose question looks like a dose question to it, an indirect warning sign looks
# like ordinary sadness. Crisis messages it set aside that contain an explicit risk word are given
# back ("rescued"), and EVERY rescued message goes to review.csv for a person to confirm.
RISK_WORDS = re.compile(
    r"(suicid|kill (him|her|my|them|your)sel|end (it|my life|his life|her life|things)|lethal|letal|deadly|"
    r"fatal|mortel|mortal|tuer|matar|morir|mourir|\bdie\b|dying|\bdead\b|overdos|never wake|not wake|"
    r"wake up again|burden|un poids|una carga|better off (dead|gone|without)|jump|bridge|wrist|rope|"
    r"\bgun\b|knife|cutting|hurt (him|her|my|them)sel|"
    r"starv|purg|throw(ing)? up|throws up|vomit|laxative|anorex|bulimi|pro-?ana|skip(ping)? meals|"
    r"hasn'?t eaten|not eating)", re.I)

# For "same template": names and pronouns are blanked, so "Can we deny Ana Ruiz's benefits?" and
# "Can we deny Tom Hale's benefits?" count as one message.
NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|M|Mme|Sr|Sra)\.?\s+[A-ZÁÉÍÓÚÑ][\w'’-]+"
                  r"|\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ'’-]+(?:,?\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ'’-]+)*")
BLANK = {"he", "she", "hes", "shes", "his", "her", "him", "they", "their", "them", "himself", "herself", "n", "s",
         "the", "a", "to", "on", "of", "that", "for", "el", "la", "le", "de", "del", "para"}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúñüàâçèêëîïôûœ ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def template(text: str) -> str:
    """The message with names, pronouns and small words blanked out."""
    words = norm(NAME.sub(" N ", text)).split()
    return " ".join(w for w in words if w not in BLANK)


def _words(text: str) -> set[str]:
    return set(text.split())


def held_out() -> list[str]:
    """Every message training must stay away from: the frozen test sets, the red-team sets, and the
    holdout of confirmed guard mistakes found by classifier/improve.py."""
    out = []
    for path in [*HELD_OUT, *([FOUND_HOLDOUT] if FOUND_HOLDOUT.exists() else [])]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                out += [norm(t) for t in row["turns"]] if "turns" in row else [norm(row["text"])]   # conversations: every turn
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
    seen, templates, kept, dropped = set(), set(), [], Counter()
    for r in rows:
        key = norm(r["text"])
        shape = (r["profile"], r.get("label"), template(r["text"]))
        if r.get("label") not in LABELS:
            dropped["bad label"] += 1
        elif key in seen:
            dropped["duplicate"] += 1
        elif shape in templates:
            dropped["same template"] += 1
        elif any(n in r["text"] for n in names):
            dropped["test-set name"] += 1
        elif too_close(key, others) >= TOO_CLOSE:
            dropped["too close to a held-out message"] += 1
        else:
            seen.add(key)
            templates.add(shape)
            kept.append({**r, "id": "g-" + hashlib.sha1(key.encode()).hexdigest()[:10]})
    return kept, dropped


def rescue(rejected: list[dict]) -> list[dict]:
    """Crisis messages the second look set aside that still contain an explicit risk word."""
    out = []
    for r in rejected:
        if r.get("label") == "crisis" and RISK_WORDS.search(r["text"]):
            out.append({k: v for k, v in r.items() if k != "judge"} | {"rescued": True})
    return out


def read_reviewed() -> dict[str, dict]:
    """Every review recorded so far, by message id."""
    if not REVIEWED.exists():
        return {}
    with REVIEWED.open(encoding="utf-8") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def apply_decisions(rows: list[dict], reviewed: dict[str, dict]) -> tuple[list[dict], int]:
    """Corrected labels and drops from the reviews. Returns the rows and how many were dropped."""
    out, dropped = [], 0
    for r in rows:
        seen = reviewed.get(r["id"])
        if seen is None:
            out.append(r)
        elif seen["your_label"] == "drop":
            dropped += 1
        elif seen["your_label"]:
            out.append({**r, "label": seen["your_label"], "checked": "corrected"})
        else:
            out.append({**r, "checked": "confirmed"})
    return out, dropped


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


def write_review(rows: list[dict], seed: int, per_cell: int = REVIEW_PER_CELL) -> int:
    """What still needs a look: unreviewed rescued messages, and a sample from each profile, label and
    angle that has no reviewed message yet. Returns how many went into review.csv."""
    rng = random.Random(seed + 1)
    sample = [r for r in rows if r.get("rescued") and "checked" not in r]
    cells: dict[tuple, list] = {}
    for r in rows:
        if not r.get("rescued"):
            cells.setdefault((r["profile"], r["label"], r["angle"]), []).append(r)
    for key in sorted(cells):
        done = sum(1 for r in cells[key] if "checked" in r)
        todo = [r for r in cells[key] if "checked" not in r]
        if done == 0:                                   # cells reviewed before keep their sample
            sample += rng.sample(todo, min(per_cell, len(todo)))
    with REVIEW.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(REVIEW_FIELDS)
        for r in sample:
            angle = r["angle"] + (" (rescued)" if r.get("rescued") else "")
            w.writerow([r["id"], r["profile"], angle, r["label"], r["text"], "", ""])
    REVIEW.with_name("review_progress.json").write_text(   # a new sample starts a new review
        json.dumps({"done": 0, "total": len(sample)}))
    return len(sample)


def apply_review(reviewer: str) -> None:
    from classifier.review import finished
    if not reviewer.strip():
        raise SystemExit('Say who reviewed it: --reviewer "Claude (claude-opus-5-5); checked by NAME"')
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

    reviewed = read_reviewed()
    for r in rows:
        reviewed[r["id"]] = {"id": r["id"], "profile": r["profile"], "label": r["label"],
                             "your_label": changed.get(r["id"], ""), "reviewer": reviewer.strip(),
                             "note": r["note"]}
    with REVIEWED.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEWED_FIELDS)
        w.writeheader()
        w.writerows(reviewed.values())

    for path in (TRAIN, DEV):
        write_jsonl(path, apply_decisions(read_jsonl(path), reviewed)[0])
    if rows:
        right = len(rows) - len(changed)
        print(f"Reviewed {len(rows)}: generator label right for {right} ({right / len(rows):.0%}), "
              f"corrected {sum(v != 'drop' for v in changed.values())}, "
              f"dropped {sum(v == 'drop' for v in changed.values())}")
        by = Counter((r["label"], changed.get(r["id"], r["label"])) for r in rows)
        for (was, now), n in sorted(by.items()):
            if was != now:
                print(f"  {was:8} -> {now:8} {n}")
    right = sum(1 for r in reviewed.values() if not r["your_label"])
    print(f"All reviews so far: {len(reviewed)} messages, generator label right for {right} "
          f"({right / max(1, len(reviewed)):.0%}). Recorded in {REVIEWED.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply-review", action="store_true")
    ap.add_argument("--reviewer", default="", help="who reviewed review.csv (with --apply-review)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--per-cell", type=int, default=REVIEW_PER_CELL,
                    help="messages to review from each new kind of message")
    args = ap.parse_args()
    if args.apply_review:
        apply_review(args.reviewer)
        return
    rescued = rescue(read_jsonl(REJECTED)) if REJECTED.exists() else []
    rows, dropped = clean(read_jsonl(RAW) + rescued, held_out(), test_names())
    rows, n_drop = apply_decisions(rows, read_reviewed())
    if n_drop:
        dropped["dropped in review"] = n_drop
    train, dev = split(rows, args.seed)
    write_jsonl(TRAIN, train)
    write_jsonl(DEV, dev)
    n_review = write_review(train + dev, args.seed, args.per_cell)
    print(f"Kept {len(rows)}: train {len(train)}, dev {len(dev)}. Dropped: {dict(dropped) or 'none'}")
    print(f"Rescued from rejected.jsonl: {sum(1 for r in rows if r.get('rescued'))} crisis messages. "
          f"Reviewed so far: {sum(1 for r in rows if 'checked' in r)}")
    for profile in sorted({r['profile'] for r in rows}):
        c = Counter(r["label"] for r in rows if r["profile"] == profile)
        print(f"  {profile:17} " + "  ".join(f"{k} {c[k]}" for k in LABELS))
    if n_review:
        print(f"{n_review} messages to review: uv run python -m classifier.review, then "
              'uv run python -m classifier.clean --apply-review --reviewer "NAME"')
    else:
        print("Nothing new to review.")


if __name__ == "__main__":
    assert TEST_SET.exists()
    main()