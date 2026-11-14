"""Score the sensitive-category detector on a development set (ADR 038).

    uv run python -m eval.run_sensitivity                       # the lists alone: the baseline
    uv run python -m eval.run_sensitivity --sets my_sets.yaml   # a deployment's own value sets
    uv run python -m eval.run_sensitivity --model qwen2.5:7b    # the lists and a local model (Ollama running)
    uv run python -m eval.run_sensitivity --model qwen2.5:7b --model-only   # the model without the lists

The set (described in eval/datasets/sensitivity_dev_v1_protocol.md) holds synthetic text
(sensitivity_dev_v1.jsonl) and FHIR resources (sensitivity_dev_v1_resources.json), each with the
categories it should get. The resources are a .json file because every .jsonl in eval/datasets is
read as text the guard classifier must not train on (classifier/clean.py). It is a DEVELOPMENT set:
it may be rerun, it was written by the author of the lists, and its numbers are a baseline for a
text model to beat, not evidence about real records.

Reported per category: how many of its rows were caught, split by how the text names it
(explicit wording, implied only, coded resource), and how many rows without it were marked anyway.
Then how often rows with no category were marked at all, by kind of row.

Output, in eval/results/ (commit it): sensitivity_dev_v1_<detector>_<value sets hash>.json with every
prediction, and a .md table.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from privacy import sensitivity

DATASETS = Path(__file__).resolve().parent / "datasets"
RESULTS = Path(__file__).resolve().parent / "results"
DEV = DATASETS / "sensitivity_dev_v1.jsonl"
POSITIVE = ("explicit", "implicit", "resource")            # strata whose rows have a category
NEGATIVE = ("hard_negative", "plain", "resource_negative")  # strata whose rows have none


def resources_file(dataset: Path) -> Path:
    return dataset.with_name(dataset.stem + "_resources.json")


def read_rows(dataset: Path, known: set[str]) -> list[dict]:
    """The text rows (one JSON object per line) and, when the file exists, the resource rows beside them."""
    found = [(f"{dataset.name} line {n}", json.loads(line))
             for n, line in enumerate(dataset.read_text(encoding="utf-8").splitlines(), 1) if line.strip()]
    beside = resources_file(dataset)
    if beside.exists():
        found += [(f"{beside.name} row {n}", row) for n, row in enumerate(json.loads(beside.read_text(encoding="utf-8")), 1)]
    rows = []
    for where, row in found:
        if not isinstance(row, dict) or row.get("kind") not in ("text", "resource") or row["kind"] not in row:
            raise ValueError(f"{where}: needs kind 'text' or 'resource' and a field of that name")
        if row.get("stratum") not in POSITIVE + NEGATIVE:
            raise ValueError(f"{where}: unknown stratum {row.get('stratum')!r}")
        categories = row.get("categories")
        if not isinstance(categories, list):
            raise ValueError(f"{where}: 'categories' must be a list (empty for none)")
        unknown = set(categories) - known
        if unknown:
            raise ValueError(f"{where}: categories not in the value sets: {', '.join(sorted(unknown))}")
        if bool(categories) != (row["stratum"] in POSITIVE):
            raise ValueError(f"{where}: stratum {row['stratum']} does not fit categories {categories}")
        rows.append(row)
    return rows


def predict(detector: sensitivity.Detector, row: dict) -> list[sensitivity.Finding]:
    return detector.of_text(row["text"]) if row["kind"] == "text" else detector.of_resource(row["resource"])


def score(rows: list[dict], predictions: list[tuple[str, ...]], category_keys: list[str]) -> dict:
    """Counts only: the set is too small for rates to mean much beyond the counts."""
    per_category = {}
    for c in category_keys:
        caught, total = Counter(), Counter()
        false = 0
        for row, got in zip(rows, predictions):
            if c in row["categories"]:
                total[row["stratum"]] += 1
                caught[row["stratum"]] += c in got
            else:
                false += c in got
        per_category[c] = {"caught": dict(caught), "total": dict(total), "marked_without_it": false,
                           "rows_without_it": sum(c not in r["categories"] for r in rows)}
    negatives = {s: {"marked": sum(bool(g) for r, g in zip(rows, predictions) if r["stratum"] == s),
                     "total": sum(r["stratum"] == s for r in rows)} for s in NEGATIVE}
    exact = sum(tuple(sorted(r["categories"])) == g for r, g in zip(rows, predictions))
    return {"rows": len(rows), "exactly_right": exact, "per_category": per_category, "negatives": negatives}


def table(result: dict, titles: dict[str, str]) -> str:
    def cell(d: dict, stratum: str) -> str:
        return f"{d['caught'].get(stratum, 0)}/{d['total'][stratum]}" if d["total"].get(stratum) else "–"

    lines = ["| Category | Explicit wording caught | Implied only caught | Coded resources caught | "
             "Marked on rows without it |", "|---|---|---|---|---|"]
    for c, d in result["per_category"].items():
        if not sum(d["total"].values()) and not d["marked_without_it"]:
            continue
        lines.append(f"| {titles[c]} | {cell(d, 'explicit')} | {cell(d, 'implicit')} | {cell(d, 'resource')} | "
                     f"{d['marked_without_it']}/{d['rows_without_it']} |")
    n = result["negatives"]
    lines += ["", "| Rows with no category | Marked anyway |", "|---|---|",
              f"| Look sensitive but are not (hard negatives) | {n['hard_negative']['marked']}/{n['hard_negative']['total']} |",
              f"| Everyday text | {n['plain']['marked']}/{n['plain']['total']} |",
              f"| Resources | {n['resource_negative']['marked']}/{n['resource_negative']['total']} |",
              "", f"Rows with exactly the right categories: {result['exactly_right']}/{result['rows']}."]
    return "\n".join(lines)


def run(sets_path: Path = sensitivity.DEFAULT, dataset: Path = DEV, model=None, name: str = "lists",
        lists: bool = True, progress=None) -> dict:
    """`model`: a text model (privacy.sensitivity_model) added to the lists, or measured alone with
    lists=False. `progress(done, total)` is called as rows finish: a model takes a second a row."""
    sets = sensitivity.load(sets_path)
    detector = sensitivity.Detector(sets, model=model, lists=lists)
    rows = read_rows(dataset, set(sets.categories))
    findings = []
    for n, r in enumerate(rows, 1):
        findings.append(predict(detector, r))
        if progress:
            progress(n, len(rows))
    predictions = [sensitivity.categories(f) for f in findings]
    result = score(rows, predictions, list(sets.categories))
    return {"dataset": dataset.name, "detector": name, "value_sets_sha256": sets.sha256,
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"), "summary": result,
            "titles": {k: c.title for k, c in sets.categories.items()},
            "predictions": [{"id": r["id"], "stratum": r["stratum"], "expected": sorted(r["categories"]),
                             "got": list(p), "evidence": sensitivity.summary(f)}
                            for r, p, f in zip(rows, predictions, findings)]}


def main() -> None:
    ap = argparse.ArgumentParser(description="Score the sensitive-category detector on the development set.")
    ap.add_argument("--sets", type=Path, default=sensitivity.DEFAULT, help="value sets file")
    ap.add_argument("--dataset", type=Path, default=DEV)
    ap.add_argument("--no-save", action="store_true", help="print only")
    ap.add_argument("--model", help="a local model served by Ollama, e.g. qwen2.5:7b, read with the lists")
    ap.add_argument("--model-only", action="store_true", help="with --model: the model without the lists")
    args = ap.parse_args()
    if args.model_only and not args.model:
        ap.error("--model-only needs --model")
    reader, name = None, "lists"
    if args.model:
        from privacy import sensitivity_model
        reader = sensitivity_model.local_reader(args.model, sensitivity.load(args.sets))
        tag = re.sub(r"[^A-Za-z0-9.]+", "-", args.model)
        name = tag if args.model_only else f"lists+{tag}"

    def progress(done: int, total: int) -> None:
        if reader is not None and (done % 20 == 0 or done == total):
            print(f"  {done}/{total} rows", flush=True)
    out = run(args.sets, args.dataset, reader, name, lists=not args.model_only, progress=progress)
    if reader is not None and reader.calls:
        out["model"] = {"name": args.model, "calls": reader.calls, "unreadable": reader.unreadable,
                        "median_seconds": round(statistics.median(reader.seconds), 2)}
    md = (f"# Sensitive categories: {out['detector']} on {out['dataset']}\n\n"
          f"Development set, rerunnable. Value sets {out['value_sets_sha256'][:12]}. {out['run_at']}.\n\n"
          + table(out["summary"], out["titles"]) + "\n")
    if "model" in out:
        m = out["model"]
        md += (f"\nModel {m['name']}: {m['calls']} calls, median {m['median_seconds']} s each, "
               f"{m['unreadable']} replies that could not be read.\n")
    print(md)
    wrong = [p for p in out["predictions"] if p["expected"] != p["got"]]
    print(f"{len(wrong)} rows not exactly right:")
    for p in wrong:
        print(f"  {p['id']} ({p['stratum']}): expected {p['expected'] or 'none'}, got {p['got'] or 'none'}")
    if not args.no_save:
        RESULTS.mkdir(parents=True, exist_ok=True)
        path = RESULTS / f"sensitivity_dev_v1_{out['detector']}_{out['value_sets_sha256'][:12]}.json"
        path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        path.with_suffix(".md").write_text(md, encoding="utf-8")
        print(f"\nSaved {path.relative_to(RESULTS.parent.parent)} and the .md table. Commit both.")


if __name__ == "__main__":
    main()
