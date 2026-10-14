"""Review the sample in review.csv one message at a time, in the terminal.

    uv run python -m classifier.review
    uv run python -m classifier.review --file classifier/data/found/round_001/review.csv   # a self-improvement round

For each message it shows the profile, the kind of message and the label it was given.
Press a key, then Enter:

    Enter   keep: the label is right (or keep the answer already there)
    a       it is allow
    d       it is decision
    c       it is crisis
    x       drop it (nonsense or unusable)
    b       go back one message
    q       save and quit (run again later to continue where you stopped)

Your answers are written to review.csv (your_label) as you go. When every message has an
answer, run:  uv run python -m classifier.clean --apply-review --reviewer "NAME"
(for a self-improvement round: uv run python -m classifier.improve accept --round N --reviewer "NAME")
Judge by docs/classifier/labels.md: crisis wins; reading a record is allow; medication
amounts are decision in the health profiles.
"""

import argparse
import csv
import json
from pathlib import Path

from classifier import clean

KEYS = {"a": "allow", "d": "decision", "c": "crisis", "x": "drop"}
HELP = ("Enter = keep · a = allow · d = decision · c = crisis · x = drop · b = back · q = save and quit\n"
        "Judge by docs/classifier/labels.md: crisis wins; reading a record is allow; "
        "medication amounts are decision in health profiles.")


FILE: Path | None = None             # --file: another review file; default classifier/data/review.csv


def review_file() -> Path:
    return FILE or clean.REVIEW


def progress_file():
    """Next to the review file: how far the review has got."""
    return review_file().with_name("review_progress.json")


def load() -> tuple[list[dict], int]:
    with review_file().open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    done = json.loads(progress_file().read_text())["done"] if progress_file().exists() else 0
    return rows, min(done, len(rows))


def save(rows: list[dict], done: int) -> None:
    with review_file().open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "profile", "angle", "label", "text", "your_label", "note"])
        w.writeheader()
        w.writerows(rows)
    progress_file().write_text(json.dumps({"done": done, "total": len(rows)}))


def answer(row: dict, key: str) -> bool:
    """Apply one key to a row. False if the key means nothing. Enter keeps what is there: the
    given label, or an answer already filled in (yours from before, or a first pass)."""
    key = key.strip().lower()
    if key == "":
        pass
    elif key in KEYS:
        row["your_label"] = "" if KEYS[key] == row["label"] else KEYS[key]
    else:
        return False
    return True


def finished() -> bool:
    """True if every message has been looked at (used by clean.py --apply-review)."""
    if not progress_file().exists():
        return False
    p = json.loads(progress_file().read_text())
    return p["done"] >= p["total"]


def main(file: Path | None = None) -> None:
    global FILE
    FILE = file
    rows, i = load()
    print(HELP)
    while i < len(rows):
        r = rows[i]
        print(f"\n[{i + 1}/{len(rows)}] {r['profile']} · {r['angle']}")
        print(f"  {r['text']}")
        print(f"  label: {r['label']}" + (f"   -> changed to: {r['your_label']}" if r["your_label"] else ""))
        if r.get("note"):
            print(f"  note: {r['note']}")
        key = input("  > ")
        if key.strip().lower() == "q":
            break
        if key.strip().lower() == "b":
            i = max(0, i - 1)
            continue
        if not answer(r, key):
            print("  Enter, a, d, c, x, b or q")
            continue
        i += 1
        save(rows, i)
    save(rows, i)
    changed = sum(1 for r in rows if r["your_label"])
    if i >= len(rows):
        nxt = ('uv run python -m classifier.improve accept --round N --reviewer "NAME"' if FILE
               else 'uv run python -m classifier.clean --apply-review --reviewer "NAME"')
        print(f"\nDone: {len(rows)} reviewed, {changed} changed. Next:\n  {nxt}")
    else:
        print(f"\nSaved at {i}/{len(rows)}. Run again to continue.")


def cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", type=Path, default=None, help="review file (default classifier/data/review.csv)")
    main(ap.parse_args().file)


if __name__ == "__main__":
    cli()