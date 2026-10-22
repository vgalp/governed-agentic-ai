"""Build the independent crisis benchmark from three public datasets (crisis_benchmark_v1_protocol.md).

    uv run --group train python -m eval.build_crisis_benchmark

Every message and every label in it was written by someone else: researchers and annotators outside
this project. This script only downloads the three sources at pinned versions, checks their hashes,
and applies the mapping rule written in the protocol before anything was scored:

  expected crisis      Aegis 2.0 test prompts labelled unsafe in "Suicide and Self Harm";
                       SimpleSafetyTests prompts in "Suicide, Self-Harm, and Eating Disorders".
  expected not crisis  Aegis 2.0 test prompts labelled safe; XSTest's 250 safe prompts.
  not used             everything else (other unsafe categories, XSTest's unsafe contrasts):
                       whether they are a crisis would be this project's judgment, not the source's.

Downloads go to eval/external/raw/ (not committed). The benchmark, eval/datasets/crisis_benchmark_v1.jsonl,
is committed with its sources and licences recorded in crisis_benchmark_v1_sources.json. Because it
sits in eval/datasets/, classifier/clean.py keeps every training message away from it, like the
frozen test sets. Once the protocol's Freeze section has its SHA-256, this script refuses to
overwrite it.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "eval" / "datasets"
RAW = ROOT / "eval" / "external" / "raw"
BENCHMARK = DATASETS / "crisis_benchmark_v1.jsonl"
MANIFEST = DATASETS / "crisis_benchmark_v1_sources.json"
PROTOCOL = DATASETS / "crisis_benchmark_v1_protocol.md"

SOURCES = {
    "aegis2": {
        "name": "Aegis AI Content Safety Dataset 2.0 (NVIDIA), test split",
        "repo": "nvidia/Aegis-AI-Content-Safety-Dataset-2.0", "file": "test.json",
        "revision": None,              # pinned on first build: the commit is recorded in the manifest
        "licence": "CC BY 4.0",
        "url": "https://huggingface.co/datasets/nvidia/Aegis-AI-Content-Safety-Dataset-2.0",
        "citation": "Ghosh et al., AEGIS2.0: A Diverse AI Safety Dataset and Risks Taxonomy for Alignment of "
                    "LLM Guardrails, NAACL 2025.",
        "labels_by": "human annotators (prompt labels); prompts written by people (Anthropic HH-RLHF red-teaming, "
                     "DAN, AI-assisted red-teaming)",
    },
    "sst": {
        "name": "SimpleSafetyTests",
        "repo": "bertiev/SimpleSafetyTests", "file": "SimpleSafetyTests - test cases.csv",
        "revision": "d7aee9a9422a5a5488f478fd79c2479c891c0f3b",
        "sha256": "6d95a1301e0d0f3a3c4cf5392f4afff11ad6e3066f95d23aaa138d44aedf986c",
        "licence": "CC BY 4.0",
        "url": "https://github.com/bertiev/SimpleSafetyTests",
        "citation": "Vidgen et al., SimpleSafetyTests: a Test Suite for Identifying Critical Safety Risks in "
                    "Large Language Models, 2023 (arXiv:2311.08370).",
        "labels_by": "the authors (handcrafted prompts, each written for one harm area)",
    },
    "xstest": {
        "name": "XSTest",
        "repo": "paul-rottger/xstest", "file": "xstest_prompts.csv",
        "revision": "d7bb5bd738c1fcbc36edd83d5e7d1b71a3e2d84d",
        "sha256": "11783fb294ed017473ee53c207d71f2161c7672c8d0b037501e78387f801cb5a",
        "licence": "CC BY 4.0",
        "url": "https://github.com/paul-rottger/xstest",
        "citation": "Röttger et al., XSTest: A Test Suite for Identifying Exaggerated Safety Behaviours in Large "
                    "Language Models, NAACL 2024.",
        "labels_by": "the authors (handcrafted safe prompts and unsafe contrasts)",
    },
}
SELF_HARM_AEGIS = "suicide and self harm"
SELF_HARM_SST = "Suicide, Self-Harm, and Eating Disorders"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def frozen_hash(protocol: Path = PROTOCOL) -> str | None:
    """The benchmark's SHA-256 from the protocol's Freeze section, or None while it says pending."""
    if not protocol.exists():
        return None
    m = re.search(r"`crisis_benchmark_v1\.jsonl` SHA-256:\s*([0-9a-f]{64})", protocol.read_text(encoding="utf-8"))
    return m.group(1) if m else None


# --- mapping (pure: tested without network) --------------------------------------------------------

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def categories(value) -> list[str]:
    """Aegis violated_categories as a list of lower-case names, hyphens read as spaces."""
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [re.sub(r"[\s-]+", " ", str(c)).strip().lower() for c in items if str(c).strip()]


def aegis_rows(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        text = (r.get("prompt") or "").strip()
        if not text or text == "REDACTED":                 # redacted prompts are not distributed
            continue
        label, cats = (r.get("prompt_label") or "").strip().lower(), categories(r.get("violated_categories"))
        if label == "unsafe" and SELF_HARM_AEGIS in cats:
            expected = "crisis"
        elif label == "safe":
            expected = "not_crisis"
        else:
            continue
        out.append({"id": f"aegis2-{r['id']}", "source": "aegis2", "text": text, "expected": expected,
                    "source_label": f"{label}: {', '.join(cats)}" if cats else label})
    return out


def sst_rows(rows: list[dict]) -> list[dict]:
    return [{"id": r["id"], "source": "sst", "text": r["prompts_final"].strip(), "expected": "crisis",
             "source_label": f"{r['harm_area']}: {r['category']}"}
            for r in rows if r["harm_area"].strip() == SELF_HARM_SST]


def xstest_rows(rows: list[dict]) -> list[dict]:
    return [{"id": f"xstest-{r['id']}", "source": "xstest", "text": r["prompt"].strip(), "expected": "not_crisis",
             "source_label": f"safe: {r['type']}"}
            for r in rows if r["label"].strip() == "safe"]


def dedupe(rows: list[dict]) -> tuple[list[dict], Counter]:
    """One row per message. The same message with conflicting expectations is dropped entirely."""
    by_text: dict[str, list[dict]] = {}
    for r in rows:
        by_text.setdefault(norm(r["text"]), []).append(r)
    kept, dropped = [], Counter()
    for group in by_text.values():
        if len({r["expected"] for r in group}) > 1:
            dropped["conflicting labels"] += len(group)
            continue
        kept.append(group[0])
        dropped["duplicate"] += len(group) - 1
    return kept, dropped


# --- downloads -------------------------------------------------------------------------------------

def github_file(src: dict) -> bytes:
    url = (f"https://raw.githubusercontent.com/{src['repo']}/{src['revision']}/"
           + urllib.parse.quote(src["file"]))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "governed-agentic-ai"}),
                                timeout=60) as r:
        data = r.read()
    if sha256_bytes(data) != src["sha256"]:
        sys.exit(f"{src['name']}: the file at {src['revision'][:12]} has SHA-256 {sha256_bytes(data)[:12]}, "
                 f"not the pinned {src['sha256'][:12]}. Nothing was built.")
    return data


def aegis_file(src: dict, revision: str | None) -> tuple[bytes, str]:
    from huggingface_hub import HfApi, hf_hub_download      # comes with transformers (train group)
    rev = revision or HfApi().dataset_info(src["repo"], revision="main").sha
    path = hf_hub_download(src["repo"], src["file"], repo_type="dataset", revision=rev,
                           local_dir=str(RAW / "aegis2"))
    return Path(path).read_bytes(), rev


def read_json_records(data: bytes) -> list[dict]:
    text = data.decode("utf-8")
    try:
        loaded = json.loads(text)
        return loaded if isinstance(loaded, list) else loaded.get("data", [])
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def training_overlap(rows: list[dict]) -> dict:
    """How close any benchmark message is to the guard's current training and dev messages
    (classifier/clean.py's measure). Expected: nothing at 0.75 or above."""
    from classifier.clean import DEV, TRAIN, TOO_CLOSE, _words, norm as cnorm, too_close
    seen = []
    for path in (TRAIN, DEV):
        if path.exists():
            seen += [cnorm(json.loads(line)["text"]) for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
    others = [(t, _words(t)) for t in seen]
    exact = set(seen)
    close = []
    for r in rows:
        t = cnorm(r["text"])
        score = 1.0 if t in exact else too_close(t, others)
        if score >= TOO_CLOSE:
            close.append({"id": r["id"], "similarity": round(score, 2)})
    return {"training_messages_compared": len(seen), "threshold": TOO_CLOSE, "too_close": close}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aegis-revision", default=None,
                    help="Hugging Face commit of the Aegis dataset (default: the one in the manifest, else main)")
    args = ap.parse_args()
    if frozen_hash() and BENCHMARK.exists():
        sys.exit(f"{BENCHMARK.name} is frozen (SHA-256 in {PROTOCOL.name}). The protocol does not allow "
                 "rebuilding it: a new benchmark is a new version.")
    RAW.mkdir(parents=True, exist_ok=True)
    previous = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    aegis_rev = args.aegis_revision or (previous.get("sources", {}).get("aegis2") or {}).get("revision")

    print("Downloading the three sources at pinned versions")
    sst_data, xs_data = github_file(SOURCES["sst"]), github_file(SOURCES["xstest"])
    (RAW / "sst.csv").write_bytes(sst_data)
    (RAW / "xstest.csv").write_bytes(xs_data)
    aegis_data, aegis_rev = aegis_file(SOURCES["aegis2"], aegis_rev)

    rows = (aegis_rows(read_json_records(aegis_data))
            + sst_rows(list(csv.DictReader(io.StringIO(sst_data.decode("utf-8")))))
            + xstest_rows(list(csv.DictReader(io.StringIO(xs_data.decode("utf-8"))))))
    rows, dropped = dedupe(rows)
    rows.sort(key=lambda r: (r["source"], r["expected"], r["id"]))

    BENCHMARK.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    counts = Counter((r["source"], r["expected"]) for r in rows)
    overlap = training_overlap(rows)
    sources = {k: dict(v) for k, v in SOURCES.items()}
    sources["aegis2"].update(revision=aegis_rev, sha256=sha256_bytes(aegis_data))
    MANIFEST.write_text(json.dumps({
        "benchmark": BENCHMARK.name, "built": date.today().isoformat(),
        "benchmark_sha256": sha256_bytes(BENCHMARK.read_bytes()),
        "sources": sources,
        "counts": {f"{s} {e}": n for (s, e), n in sorted(counts.items())},
        "dropped": dict(dropped),
        "training_overlap": overlap,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n{len(rows)} messages ({dict(dropped) or 'none dropped'}):")
    for (s, e), n in sorted(counts.items()):
        print(f"  {s:8} {e:11} {n}")
    print(f"\nCompared with {overlap['training_messages_compared']} training and dev messages: "
          f"{len(overlap['too_close'])} at similarity {overlap['threshold']} or more"
          + (f": {overlap['too_close'][:10]}" if overlap["too_close"] else "."))
    print(f"\nWrote {BENCHMARK.relative_to(ROOT)} and {MANIFEST.relative_to(ROOT)}.")
    print("Review the counts, then put these lines in the protocol's Freeze section and commit:")
    print(f"- `crisis_benchmark_v1.jsonl` SHA-256: {sha256_bytes(BENCHMARK.read_bytes())}")
    print(f"- Aegis 2.0 revision: {aegis_rev}")


if __name__ == "__main__":
    main()
