"""Build independent crisis benchmark v2 from three public datasets (crisis_benchmark_v2_protocol.md).

    uv run --group train --with pyarrow python -m eval.build_crisis_benchmark_v2

Before the first build, accept AI2's Responsible Use Guidelines on
https://huggingface.co/datasets/allenai/wildguardmix with the Hugging Face account you are logged
in with (`hf auth login`). The token is read by huggingface_hub, never by this script.

Every message and every label was written by someone else. This script downloads the sources at
pinned versions, checks their hashes, and applies the mapping rule fixed in the protocol:

  expected crisis      OpenAI moderation set, SH = 1;
                       WildGuardTest, harmful, subcategory "Mental Health & Over-Reliance Crisis";
                       OR-Bench-Toxic, category self-harm.
  expected not crisis  WildGuardTest, unharmful; OR-Bench-Hard-1K, category self-harm.
  not used             everything else, including OpenAI texts with SH = 0 (the protocol says why).

Then it drops duplicates, conflicts, and anything identical or close (0.75) to a benchmark v1 message.

Writes eval/datasets/crisis_benchmark_v2.jsonl (NOT committed: WildGuardTest is shared only after
accepting AI2's terms), crisis_benchmark_v2_ids.jsonl (ids, labels and text hashes: committed) and
crisis_benchmark_v2_sources.json (committed). Once the protocol's Freeze section has the SHA-256,
the script refuses to overwrite the benchmark; on another machine it rebuilds the same file and
checks the hash.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

from eval.build_crisis_benchmark import BENCHMARK as V1_BENCHMARK
from eval.build_crisis_benchmark import dedupe

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "eval" / "datasets"
RAW = ROOT / "eval" / "external" / "raw"
BENCHMARK = DATASETS / "crisis_benchmark_v2.jsonl"
IDS = DATASETS / "crisis_benchmark_v2_ids.jsonl"
MANIFEST = DATASETS / "crisis_benchmark_v2_sources.json"
PROTOCOL = DATASETS / "crisis_benchmark_v2_protocol.md"
PRIMARY = ("openai", "wildguard")          # human-labelled sources: the primary result
V1_TOO_CLOSE = 0.75

SOURCES = {
    "openai": {
        "name": "OpenAI moderation evaluation set (samples-1680)",
        "repo": "openai/moderation-api-release", "file": "data/samples-1680.jsonl.gz",
        "revision": "f4ab51b5edd3bfbcb349a56324274235b674e0e4",
        "sha256": "ef3f7de86ee394fe337b0a566ba4b9c7db058f3caa8d6a4a0006138b663ab9a8",
        "licence": "MIT",
        "url": "https://github.com/openai/moderation-api-release",
        "citation": "Markov et al., A Holistic Approach to Undesired Content Detection in the Real World, "
                    "AAAI 2023 (arXiv:2208.03274).",
        "labels_by": "OpenAI's labellers",
    },
    "wildguard": {
        "name": "WildGuardTest (test split of WildGuardMix, Allen Institute for AI)",
        "repo": "allenai/wildguardmix", "revision": None,     # pinned on first build, in the manifest
        "licence": "ODC-BY; access after accepting AI2's Responsible Use Guidelines (not redistributed here)",
        "url": "https://huggingface.co/datasets/allenai/wildguardmix",
        "citation": "Han et al., WildGuard: Open One-Stop Moderation Tools for Safety Risks, Jailbreaks, and "
                    "Refusals of LLMs, NeurIPS 2024 Datasets and Benchmarks (arXiv:2406.18495).",
        "labels_by": "three human annotators per item",
    },
    "orbench": {
        "name": "OR-Bench (Hard-1K and Toxic)",
        "repo": "bench-llm/or-bench", "revision": None,       # pinned on first build, in the manifest
        "licence": "CC BY 4.0",
        "url": "https://huggingface.co/datasets/bench-llm/or-bench",
        "citation": "Cui et al., OR-Bench: An Over-Refusal Benchmark for Large Language Models, 2024 "
                    "(arXiv:2405.20947).",
        "labels_by": "an ensemble of three language models (not people); reported apart, not in the primary result",
    },
}
OPENAI_CATEGORIES = ("S", "H", "V", "HR", "SH", "S3", "H2", "V2")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def text_hash(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def frozen_hash(protocol: Path = PROTOCOL) -> str | None:
    """The benchmark's SHA-256 from the protocol's Freeze section, or None while it says pending."""
    if not protocol.exists():
        return None
    m = re.search(r"`crisis_benchmark_v2\.jsonl` SHA-256:\s*([0-9a-f]{64})", protocol.read_text(encoding="utf-8"))
    return m.group(1) if m else None


# --- mapping (pure: tested without network) --------------------------------------------------------

def openai_rows(records: list[dict]) -> list[dict]:
    """SH = 1 is crisis. SH = 0 is not used: the label is about content, not the writer's risk."""
    out = []
    for i, r in enumerate(records):
        text = (r.get("prompt") or "").strip()
        if text and r.get("SH") == 1:
            flags = [k for k in OPENAI_CATEGORIES if r.get(k) == 1]
            out.append({"id": f"openai-{i:04d}", "source": "openai", "text": text, "expected": "crisis",
                        "source_label": "flagged: " + ", ".join(flags)})
    return out


def subcategory_key(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def is_crisis_subcategory(value) -> bool:
    key = subcategory_key(value)
    return "mental health" in key and "crisis" in key


def wildguard_rows(records: list[dict]) -> tuple[list[dict], list[str]]:
    """Harmful in the mental-health crisis subcategory is crisis; unharmful is not crisis.
    Returns the rows and the distinct subcategory strings that matched (the protocol wants one)."""
    out, matched = [], set()
    for i, r in enumerate(records):
        text = (r.get("prompt") or "").strip()
        label = str(r.get("prompt_harm_label") or "").strip().lower()
        if not text or label not in ("harmful", "unharmful"):
            continue
        variant = "adversarial" if r.get("adversarial") in (True, "True", "true", 1) else "plain"
        sub = r.get("subcategory")
        if label == "harmful" and is_crisis_subcategory(sub):
            matched.add(str(sub))
            expected = "crisis"
        elif label == "unharmful":
            expected = "not_crisis"
        else:
            continue
        out.append({"id": f"wildguard-{i:04d}", "source": "wildguard", "text": text, "expected": expected,
                    "variant": variant, "source_label": f"{label}: {sub}" if sub else label})
    return out, sorted(matched)


def orbench_rows(toxic: list[dict], hard: list[dict]) -> list[dict]:
    """Self-harm category only: Toxic is crisis, Hard-1K (sounds harmful, judged safe) is not."""
    out = []
    for name, rows, expected in (("toxic", toxic, "crisis"), ("hard", hard, "not_crisis")):
        for i, r in enumerate(rows):
            text = (r.get("prompt") or "").strip()
            if text and subcategory_key(r.get("category")) == "self harm":
                out.append({"id": f"orbench-{name}-{i:04d}", "source": "orbench", "text": text,
                            "expected": expected, "source_label": f"{name}: {r.get('category')}"})
    return out


def far_from(rows: list[dict], earlier: list[str], threshold: float = V1_TOO_CLOSE) -> tuple[list[dict], list[dict]]:
    """Rows not identical or close to any earlier benchmark message, and the ones dropped."""
    from classifier.clean import _words, too_close
    from classifier.clean import norm as cnorm
    exact = {cnorm(t) for t in earlier}
    others = [(t, _words(t)) for t in exact]
    kept, dropped = [], []
    for r in rows:
        t = cnorm(r["text"])
        score = 1.0 if t in exact else too_close(t, others)
        if score >= threshold:
            dropped.append({"id": r["id"], "similarity": round(score, 2)})
        else:
            kept.append(r)
    return kept, dropped


def id_rows(rows: list[dict]) -> list[dict]:
    """What is committed: everything except the text, plus the text's SHA-256."""
    return [{k: v for k, v in r.items() if k != "text"} | {"text_sha256": text_hash(r["text"])} for r in rows]


# --- downloads -------------------------------------------------------------------------------------

def openai_file(src: dict) -> list[dict]:
    url = f"https://raw.githubusercontent.com/{src['repo']}/{src['revision']}/{src['file']}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "governed-agentic-ai"}),
                                timeout=60) as r:
        data = r.read()
    if sha256_bytes(data) != src["sha256"]:
        sys.exit(f"{src['name']}: the file at {src['revision'][:12]} has SHA-256 {sha256_bytes(data)[:12]}, "
                 f"not the pinned {src['sha256'][:12]}. Nothing was built.")
    (RAW / "openai_samples-1680.jsonl.gz").write_bytes(data)
    return [json.loads(line) for line in gzip.decompress(data).decode("utf-8").splitlines() if line.strip()]


def read_table(path: Path) -> list[dict]:
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            sys.exit("Reading a .parquet file needs pyarrow: run with `uv run --group train --with pyarrow ...`.")
        return pq.read_table(path).to_pylist()
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".csv":
        return list(csv.DictReader(io.StringIO(text)))
    try:
        loaded = json.loads(text)
        return loaded if isinstance(loaded, list) else loaded.get("data", [])
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def pick(files: list[str], must: tuple[str, ...], what: str) -> str:
    found = [f for f in files if all(m in f.lower() for m in must)
             and f.lower().endswith((".parquet", ".csv", ".jsonl", ".json"))]
    if len(found) != 1:
        sys.exit(f"{what}: expected exactly one data file containing {must}, found {found or 'none'}. "
                 "Nothing was built.")
    return found[0]


def hf_files(repo: str, revision: str | None, wanted: list[tuple[tuple[str, ...], str]]) -> tuple[dict, str, dict]:
    """Download the wanted files at a pinned commit. Returns {what: rows}, the commit, {what: (file, sha256)}."""
    from huggingface_hub import HfApi, hf_hub_download      # comes with transformers (train group)
    api = HfApi()
    try:
        rev = revision or api.dataset_info(repo, revision="main").sha
        files = api.list_repo_files(repo, repo_type="dataset", revision=rev)
    except Exception as e:  # noqa: BLE001  gated, not logged in, or offline: say why and stop
        sys.exit(f"{repo}: {e}\nIf the dataset is gated, accept its terms on Hugging Face with the account "
                 "you are logged in with (`hf auth login`), then run again. Nothing was built.")
    rows, record = {}, {}
    for must, what in wanted:
        name = pick(files, must, f"{repo} {what}")
        path = Path(hf_hub_download(repo, name, repo_type="dataset", revision=rev,
                                    local_dir=str(RAW / repo.replace("/", "__"))))
        rows[what] = read_table(path)
        record[what] = (name, sha256_bytes(path.read_bytes()))
    return rows, rev, record


def training_overlap(rows: list[dict]) -> dict:
    from eval.build_crisis_benchmark import training_overlap as v1_overlap
    return v1_overlap(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wildguard-revision", default=None, help="Hugging Face commit (default: manifest, else main)")
    ap.add_argument("--orbench-revision", default=None, help="Hugging Face commit (default: manifest, else main)")
    args = ap.parse_args()
    frozen = frozen_hash()
    if frozen and BENCHMARK.exists():
        if sha256_bytes(BENCHMARK.read_bytes()) == frozen:
            sys.exit(f"{BENCHMARK.name} is already built and matches the frozen SHA-256. Nothing to do.")
        sys.exit(f"{BENCHMARK.name} exists but does not match the frozen SHA-256. Move it away and rebuild.")
    RAW.mkdir(parents=True, exist_ok=True)
    previous = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    prev_src = previous.get("sources", {})
    wg_rev = args.wildguard_revision or (prev_src.get("wildguard") or {}).get("revision")
    ob_rev = args.orbench_revision or (prev_src.get("orbench") or {}).get("revision")

    print("Downloading the three sources at pinned versions")
    oai = openai_file(SOURCES["openai"])
    wg, wg_rev, wg_files = hf_files(SOURCES["wildguard"]["repo"], wg_rev, [(("test",), "test")])
    ob, ob_rev, ob_files = hf_files(SOURCES["orbench"]["repo"], ob_rev,
                                    [(("hard", "1k"), "hard-1k"), (("toxic",), "toxic")])

    wg_rows, matched = wildguard_rows(wg["test"])
    if len(matched) != 1:
        sys.exit(f"WildGuardTest: expected exactly one mental-health crisis subcategory, found {matched or 'none'}. "
                 "The protocol's mapping cannot be applied as written. Nothing was built.")
    rows = openai_rows(oai) + wg_rows + orbench_rows(ob["toxic"], ob["hard-1k"])
    rows, dropped = dedupe(rows)
    v1_texts = [json.loads(line)["text"] for line in V1_BENCHMARK.read_text(encoding="utf-8").splitlines()
                if line.strip()] if V1_BENCHMARK.exists() else []
    if not v1_texts:
        sys.exit(f"{V1_BENCHMARK.name} is needed to drop messages close to v1. Nothing was built.")
    rows, near_v1 = far_from(rows, v1_texts)
    rows.sort(key=lambda r: (r["source"], r["expected"], r["id"]))

    if frozen:      # rebuilding on another machine: must be the same file
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        if sha256_bytes(data) != frozen:
            sys.exit(f"The rebuilt benchmark has SHA-256 {sha256_bytes(data)[:12]}, not the frozen {frozen[:12]}. "
                     "Check the pinned revisions in the manifest. Nothing was written.")
        BENCHMARK.write_bytes(data)
        print(f"Rebuilt {BENCHMARK.relative_to(ROOT)}; it matches the frozen SHA-256.")
        return

    BENCHMARK.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    IDS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in id_rows(rows)), encoding="utf-8")
    counts = Counter((r["source"], r["expected"]) for r in rows)
    overlap = training_overlap(rows)
    sources = {k: dict(v) for k, v in SOURCES.items()}
    sources["wildguard"].update(revision=wg_rev, files={k: {"file": f, "sha256": h} for k, (f, h) in wg_files.items()},
                                crisis_subcategory=matched[0])
    sources["orbench"].update(revision=ob_rev, files={k: {"file": f, "sha256": h} for k, (f, h) in ob_files.items()})
    bench_sha = sha256_bytes(BENCHMARK.read_bytes())
    MANIFEST.write_text(json.dumps({
        "benchmark": BENCHMARK.name, "built": date.today().isoformat(), "benchmark_sha256": bench_sha,
        "primary_sources": list(PRIMARY), "sources": sources,
        "counts": {f"{s} {e}": n for (s, e), n in sorted(counts.items())},
        "variants": dict(Counter(r.get("variant") for r in rows if r.get("variant"))),
        "dropped": dict(dropped) | {"close to benchmark v1": len(near_v1)},
        "close_to_v1": near_v1,
        "training_overlap": overlap,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n{len(rows)} messages; dropped: {dict(dropped) or 'no duplicates'}, {len(near_v1)} close to v1")
    for (s, e), n in sorted(counts.items()):
        print(f"  {s:10} {e:11} {n}")
    print(f"WildGuardTest crisis subcategory: {matched[0]!r}")
    print(f"\nCompared with {overlap['training_messages_compared']} training and dev messages: "
          f"{len(overlap['too_close'])} at similarity {overlap['threshold']} or more"
          + (f": {overlap['too_close'][:10]}" if overlap["too_close"] else "."))
    print(f"\nWrote {BENCHMARK.relative_to(ROOT)} (not committed), {IDS.relative_to(ROOT)} and "
          f"{MANIFEST.relative_to(ROOT)}.")
    print("Send the counts for review. Do not score anything before the freeze lines are in the protocol:")
    print(f"- `crisis_benchmark_v2.jsonl` SHA-256: {bench_sha}")
    print(f"- WildGuardMix revision: {wg_rev}")
    print(f"- OR-Bench revision: {ob_rev}")


if __name__ == "__main__":
    main()
