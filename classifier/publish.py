"""Prepare a trained guard classifier for publication, with a model card built from the record.

    uv run python -m classifier.publish --model guard-modernbert-v4 \\
        --results eval/results/guard_test_v4_classifier_f85591407ded.json
    uv run --group train python -m classifier.publish --model guard-modernbert-v4 \\
        --results eval/results/guard_test_v4_classifier_f85591407ded.json --upload vgalp/guard-modernbert-v4
    uv run python -m classifier.publish --model guard-modernbert-v6.1 \\
        --results eval/results/guard_test_v5_classifier_c58d0c88d050.json \\
        --benchmark eval/results/crisis_benchmark_v1_classifier_c58d0c88d050.json --benchmark-seen

1. Checks the model folder is complete (weights, config, tokenizer, thresholds, training_info.json).
2. Checks that every results file was scored on THESE weights: the SHA-256 of model.safetensors must
   equal the model_sha256 the frozen-test run recorded. A results file from another model is refused.
   Crisis benchmark results (--benchmark) are checked the same way, by the config the run recorded.
   --benchmark-seen says the benchmark shaped this model's training data; the card then says the
   numbers are not independent. Leave it off only if the benchmark played no part in this release.
3. Builds release/<model>/: the model files, LICENSE (Apache-2.0, as the base model and the
   generator), README.md (the model card) and MANIFEST.json (SHA-256 of every file).
   Every number in the card is read from training_info.json, the training data files, the review
   record and the results files; nothing is typed in by hand.
4. With --upload <repo>, uploads the folder to the Hugging Face Hub. The token is read from the
   HF_TOKEN environment variable (or `hf auth login`), never from a file in this repository and never
   printed. Upload as private first (--private), check the page, then make it public.

After uploading, record the repo, the commit (revision) and the weights' SHA-256 in
classifier/published.yaml so `python -m classifier.fetch` can download exactly that release.
"""

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
DATA = ROOT / "classifier" / "data"
RELEASE = ROOT / "release"
REPO_URL = "https://github.com/vgalp/governed-agentic-ai"
REQUIRED = ["config.json", "model.safetensors", "thresholds.json", "training_info.json"]
TOKENIZER = ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"]
LICENSE_TEXT = """Copyright 2026 Wladimir Galperin

Licensed under the Apache License, Version 2.0 (the "License"); you may not use these files except
in compliance with the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License
is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. See the License for the specific language governing permissions and limitations under the
License.
"""


class PublishError(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_model(model_dir: Path) -> None:
    missing = [f for f in REQUIRED if not (model_dir / f).exists()]
    if not any((model_dir / f).exists() for f in ("tokenizer.json", "tokenizer_config.json")):
        missing.append("tokenizer files")
    if missing:
        raise PublishError(f"{model_dir} is missing: {', '.join(missing)}")


def load_results(paths: list[Path], weights_sha: str) -> list[dict]:
    """Every results file must have scored exactly these weights."""
    out = []
    for p in paths:
        r = json.loads(p.read_text())
        if r.get("model_sha256") != weights_sha:
            raise PublishError(f"{p.name} was scored on weights {str(r.get('model_sha256'))[:12]}, "
                               f"not on these ({weights_sha[:12]}). Use the results of this model only.")
        out.append({**r, "_file": p.name})
    return out


def load_benchmarks(paths: list[Path], weights_sha: str) -> list[dict]:
    """Crisis benchmark results (eval/run_crisis_benchmark.py) of this classifier, and only this one:
    the run records the first 12 characters of the weights' SHA-256 as its config."""
    out = []
    for p in paths:
        r = json.loads(p.read_text())
        if r.get("system") != "classifier" or r.get("config") != weights_sha[:12]:
            raise PublishError(f"{p.name} is a run of {r.get('system')} {r.get('config')}, not of these weights "
                               f"({weights_sha[:12]}). Use the classifier's own benchmark results only.")
        m = re.search(r"crisis_benchmark_(v\d+)", p.name)
        out.append({**r, "_file": p.name, "_version": m.group(1) if m else "?"})
    return out


def rate(s: dict) -> str:
    lo, hi = s["ci95"]
    return f"{s['k']:,} / {s['n']:,} ({pct(s['rate'])}, 95% CI {pct(lo)}–{pct(hi)})"


def count(rows: list[dict], keep) -> dict:
    """k of n, with a Wilson interval, for rows chosen by `keep` (row -> None to skip, True/False)."""
    marks = [m for m in (keep(r) for r in rows) if m is not None]
    k, n = sum(marks), len(marks)
    if not n:
        return {"k": 0, "n": 0, "rate": 0.0, "ci95": [0.0, 0.0]}
    z, p = 1.96, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return {"k": k, "n": n, "rate": p, "ci95": [max(0.0, centre - half), min(1.0, centre + half)]}


def benchmark_section(benchmarks: list[dict], seen: bool) -> str:
    if not benchmarks:
        return ""
    parts = []
    for b in benchmarks:
        s, v, rows = b["summary"], b["_version"], b.get("rows", [])
        n_policies = len(s.get("policies", []))
        lines = ["| Messages | Crisis messages caught | Not-crisis messages given the crisis reply |",
                 "| --- | --- | --- |",
                 f"| All {n_policies} policies | {rate(s['all']['crisis_caught'])} | {rate(s['all']['false_crisis'])} |"]
        for key, label in (("seen_policies", "Policies seen in training"),
                           ("unseen_policies", "Policies never seen in training")):
            if key in s:
                lines.append(f"| {label} | {rate(s[key]['crisis_caught'])} | {rate(s[key]['false_crisis'])} |")
        for source, label in (("sst", "SimpleSafetyTests (crisis only)"),
                              ("xstest", "XSTest (safe prompts that sound unsafe)")):
            mine = [r for r in rows if r.get("source") == source]
            if not mine:
                continue
            caught = count(mine, lambda r: r["predicted"] == "crisis" if r["expected"] == "crisis" else None)
            false = count(mine, lambda r: r["predicted"] == "crisis" if r["expected"] != "crisis" else None)
            lines.append(f"| {label} | {rate(caught) if caught['n'] else '–'} | "
                         f"{rate(false) if false['n'] else '–'} |")
        across = s.get("across_policies", {})
        extra = ""
        if across:
            extra = (f"\n\nCrisis messages missed under every policy: "
                     f"{rate(across['crisis_missed_under_every_policy'])}. Not-crisis messages given the crisis "
                     f"reply under at least one policy: {rate(across['false_crisis_under_any_policy'])}.")
        independence = (
            f"**Not independent of this release.** The kinds of message earlier models missed or wrongly flagged "
            f"on benchmark {v} (never its messages) were used to decide what new training data to generate. "
            f"Read these numbers as results after seeing benchmark {v}. A new benchmark from different public "
            f"sources is needed for an independent measure."
            if seen else
            f"Run once. Nothing in benchmark {v} was used to train or tune this model.")
        parts.append(f"""### Crisis benchmark {v} (public data written outside this project)

Messages and labels from Aegis 2.0, SimpleSafetyTests and XSTest, each read under every policy
paragraph (`{b['_file']}`, benchmark `{str(b.get('benchmark_sha256', ''))[:12]}`, run {b.get('run_at', 'n/a')}).
These messages are not about any office's work: many are deliberately edgy, dark or off-topic, so
false alarms here are far higher than on everyday office messages. Counts are message and policy
pairs: each message is counted once under each policy.

{chr(10).join(lines)}{extra}

{independence}
""")
    return "\n".join(parts)


def data_facts(data_dir: Path) -> dict:
    """What the card says about the training data, counted from the files themselves."""
    rows = read_jsonl(data_dir / "train.jsonl") + read_jsonl(data_dir / "dev.jsonl")
    reviewed = 0
    review_file = data_dir / "reviewed.csv"
    if review_file.exists():
        reviewed = max(0, len(review_file.read_text(encoding="utf-8").strip().splitlines()) - 1)
    return {
        "rows": len(rows),
        "labels": Counter(r["label"] for r in rows),
        "profiles": Counter(r["profile"] for r in rows),
        "generators": Counter(r.get("generator", "unknown") for r in rows),
        "rejected": len(read_jsonl(data_dir / "rejected.jsonl")),
        "reviewed": reviewed,
    }


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def results_table(results: list[dict]) -> str:
    lines = ["| Frozen test set | Messages | Correct | Macro F1 | Crisis missed | Decisions let through | "
             "Everyday messages blocked | Everyday messages given the crisis reply |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        for part, label in (("all", "all profiles"), ("seen_profiles", "profiles seen in training"),
                            ("unseen_profile", "profile(s) never seen in training")):
            s = r["summary"].get(part)
            if not s:
                continue
            correct = round(s["accuracy"] * s["n"])
            lines.append(f"| {r['test_set']}, {label} | {s['n']} | {correct} / {s['n']} | {s['macro_f1']:.3f} | "
                         f"{s['crisis_missed']} / {s['crisis_total']} | {s['decisions_let_through']} / "
                         f"{s['decision_total']} | {s['wrong_blocks']} / {s['allow_total']} | "
                         f"{s['false_crisis']} / {s['allow_total']} |")
    return "\n".join(lines)


def card(model: str, info: dict, thresholds: dict, weights_sha: str, facts: dict, results: list[dict],
         benchmarks: list[dict] | None = None, benchmark_seen: bool = False) -> str:
    benchmarks = benchmarks or []
    labels = ", ".join(f"{k} {v:,}" for k, v in sorted(facts["labels"].items()))
    profiles = ", ".join(f"{k} {v:,}" for k, v in sorted(facts["profiles"].items()))
    generators = ", ".join(f"{k} ({v:,})" for k, v in facts["generators"].most_common())
    dev = info.get("best_dev", {})
    first = results[0]["summary"]["all"] if results else None
    headline = (f"On frozen test set {results[0]['test_set']} ({first['n']} messages, scored once), it missed "
                f"{first['crisis_missed']} of {first['crisis_total']} crisis messages and wrongly blocked "
                f"{first['wrong_blocks']} of {first['allow_total']} everyday messages.") if first else ""
    if benchmarks:
        b = benchmarks[0]["summary"]["all"]
        headline += (f" On public crisis benchmark {benchmarks[0]['_version']} it caught "
                     f"{pct(b['crisis_caught']['rate'])} of crisis messages and gave the crisis reply to "
                     f"{pct(b['false_crisis']['rate'])} of the others"
                     + (" (after seeing that benchmark: see Evaluation)." if benchmark_seen else "."))
    files = ", ".join(f"`{r['_file']}`" for r in results)
    false_alarms = ""
    if benchmarks:
        b = benchmarks[0]["summary"]["all"]["false_crisis"]
        false_alarms = (f"\n- **False alarms on dark or off-topic messages.** On public benchmark messages that sound "
                        f"alarming but are safe (history, fiction, definitions, rudeness), {pct(b['rate'])} got the "
                        f"crisis reply. On everyday office messages the rate is far lower, but a deployment will "
                        f"see some such hand-overs.")
    return f"""---
license: apache-2.0
base_model: {info.get('base_model', 'answerdotai/ModernBERT-base')}
pipeline_tag: text-classification
language:
- en
tags:
- guardrails
- ai-safety
- crisis-detection
- text-classification
- healthcare
- public-sector
---

# {model}

A small classifier that checks a message sent to an AI assistant, given a short description of what
that assistant may and may not do, and labels it **allow**, **decision** or **crisis**:

- **crisis**: any sign that someone may be at risk of suicide, self-harm, abuse, neglect or harm from
  others, including indirect wording. The assistant should hand the person over to a human.
- **decision**: a request for a decision the assistant must not make (for example a medication dose,
  a diagnosis, a benefits eligibility decision). The assistant should decline and point to a person.
- **allow**: everything else.

It is one layer of the open-source [Governed Agentic AI]({REPO_URL}) pipeline, which runs it after
fixed rules and before any language model sees a question. {headline}

> **Not a medical device and not a standalone safety system.** It is a research release, meant to be
> one layer among several (rules, a second classifier on answers, human review, audit). Read the
> limitations before using it.

## How it reads a message

The input is a pair: the assistant's **policy paragraph** (what it does, which decisions it must not
make, what counts as a crisis) and the **message**. The same model therefore serves different
assistants, and a new office can use it by writing its own policy paragraph, without retraining.

The label comes from the probabilities and the thresholds in `thresholds.json`, chosen on the
development set (never on a test set): crisis is checked first
(crisis probability >= {thresholds.get('crisis')}), then decision (>= {thresholds.get('decision')}),
otherwise allow.

```python
# pip install torch transformers
import json, torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

repo = "<this repository>"
tok = AutoTokenizer.from_pretrained(repo)
model = AutoModelForSequenceClassification.from_pretrained(repo).eval()
policy = "An assistant that helps adults with ADHD plan their day ... Crisis: any sign that ..."
enc = tok(policy, "I gave my things away and wrote letters to my family.",
          truncation="only_second", max_length=256, return_tensors="pt")   # never cut the policy
probs = model(**enc).logits.softmax(-1)[0]
labels = [model.config.id2label[i] for i in range(probs.numel())]
p = dict(zip(labels, probs.tolist()))
t = {thresholds}
label = "crisis" if p["crisis"] >= t["crisis"] else "decision" if p["decision"] >= t["decision"] else "allow"
```

In the pipeline it is loaded by `classifier/model.py` (`GuardModel`), which applies the same thresholds.
A message too long to fit beside the whole policy is split there into overlapping pieces, each read
with the whole policy; the highest crisis and decision probabilities count.

## Evaluation

Frozen test sets are written before the training data they test, kept out of training, recorded by
hash, and scored once per model. Some test profiles are never seen in training, to measure whether a
new office can use the model through its policy paragraph alone. Results ({files}):

{results_table(results)}

{benchmark_section(benchmarks, benchmark_seen)}
Development set (used to choose thresholds and when to stop training, not a test):
macro F1 {dev.get('macro_f1', 'n/a')}, crisis recall {dev.get('crisis', {}).get('recall', 'n/a')}.

Every frozen-set message, the model's label and its probabilities are in the results files in the
repository, with notes on each mistake.

## Training data

- **{facts['rows']:,} synthetic messages** (train and development), all generated by local models:
  {generators}. No real person's messages or records were used. The generator was asked for invented
  names, and any message using a name from a test set was dropped.
- **By label:** {labels}.
- **By assistant profile:** {profiles}.
- **Quality control:** each batch had a second look by the same generator without the intended label;
  {facts['rejected']:,} disagreements were set aside and never trained on. {facts['reviewed']:,} messages
  were checked by a person, with every correction recorded. Anything close to a frozen test message
  (similarity 0.75 or more) was removed.
- Base model: `{info.get('base_model')}`; trained {info.get('trained', 'n/a')} on {info.get('device', 'n/a')}.

## Intended use

- One input layer for staff-facing or public-facing AI assistants in health care, public services and
  similar settings: deciding whether a message should go to a person (crisis), be declined (decision),
  or be answered.
- Research on guardrails, held-out evaluation and configuration-only adoption.

## Not for

- Diagnosing anyone or assessing anyone's actual risk. A crisis label means "hand this to a person",
  never a clinical judgement.
- Use as the only safeguard, or without human follow-up on crisis labels.
- Content moderation of public platforms, or judging people.
- Languages other than English. The training data includes a few French and Spanish messages, but
  only English is supported and evaluated as such.

## Limitations

- **Synthetic data.** Real messages are messier. Results on a test set are not population rates.
- **Small test sets.** {first['crisis_total'] if first else 'n/a'} crisis messages per frozen set: with numbers this
  small, the uncertainty is wide.
- **Known misses.** Warning signs hidden inside an ordinary request (for example an ending mentioned
  after a plan) can be labelled allow with high confidence; no threshold catches them. Commands to
  make a decision ("go ahead and end the lease") can pass as allow in profiles not seen in training.
  This is why the pipeline runs fixed rules first and checks answers again.
- **Policy-dependent.** The model reads the policy paragraph: a vague policy gives weaker labels.{false_alarms}

## Files and integrity

- `model.safetensors` SHA-256: `{weights_sha}`. The results above were scored on exactly these weights.
- `MANIFEST.json` lists the SHA-256 of every file in this release.

## License and citation

Apache-2.0 (base model ModernBERT-base: Apache-2.0; training data generator Qwen2.5-7B-Instruct:
Apache-2.0). Code, test sets, results and notes: {REPO_URL}.

Galperin, W. ({info.get('trained', '2026')[:4]}). *{model}: a policy-conditioned guard classifier for crisis hand-over and
blocked decisions in AI assistants.* {REPO_URL}
"""


def build(model: str, result_paths: list[Path], models_dir: Path = MODELS, data_dir: Path = DATA,
          release_dir: Path = RELEASE, benchmark_paths: list[Path] | None = None,
          benchmark_seen: bool = False) -> Path:
    model_dir = models_dir / model
    check_model(model_dir)
    weights_sha = sha256(model_dir / "model.safetensors")
    results = load_results(result_paths, weights_sha)
    benchmarks = load_benchmarks(benchmark_paths or [], weights_sha)
    info = json.loads((model_dir / "training_info.json").read_text())
    thresholds = json.loads((model_dir / "thresholds.json").read_text())["thresholds"]

    out = release_dir / model
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for f in [*REQUIRED, *TOKENIZER, "vocab.txt", "merges.txt"]:
        if (model_dir / f).exists():
            shutil.copy2(model_dir / f, out / f)
    (out / "LICENSE").write_text(LICENSE_TEXT)
    (out / "README.md").write_text(card(model, info, thresholds, weights_sha, data_facts(data_dir), results,
                                          benchmarks, benchmark_seen))
    manifest = {p.name: sha256(p) for p in sorted(out.iterdir()) if p.is_file()}
    (out / "MANIFEST.json").write_text(json.dumps({"model": model, "files": manifest}, indent=2))
    return out


def upload(folder: Path, repo: str, private: bool) -> str:
    from huggingface_hub import HfApi                      # comes with transformers (train group)
    api = HfApi()                                          # token: HF_TOKEN or `hf auth login`
    api.create_repo(repo, repo_type="model", private=private, exist_ok=True)
    commit = api.upload_folder(folder_path=str(folder), repo_id=repo, repo_type="model",
                               commit_message=f"Release {folder.name}")
    return getattr(commit, "oid", None) or str(commit)


def main() -> None:
    ap = argparse.ArgumentParser(description="Prepare (and optionally upload) a guard classifier release.")
    ap.add_argument("--model", required=True, help="folder name under models/, e.g. guard-modernbert-v4")
    ap.add_argument("--results", nargs="+", type=Path, required=True,
                    help="frozen-test results file(s) scored on this model")
    ap.add_argument("--upload", metavar="REPO", help="Hugging Face repo id, e.g. vgalp/guard-modernbert-v4")
    ap.add_argument("--benchmark", nargs="+", type=Path, default=[],
                    help="crisis benchmark results file(s) of this model (eval/run_crisis_benchmark.py)")
    ap.add_argument("--benchmark-seen", action="store_true",
                    help="the benchmark shaped this model's training data: the card says it is not independent")
    ap.add_argument("--private", action="store_true", help="create the Hugging Face repo as private")
    args = ap.parse_args()
    try:
        out = build(args.model, args.results, benchmark_paths=args.benchmark,
                    benchmark_seen=args.benchmark_seen)
    except PublishError as e:
        raise SystemExit(f"Not published: {e}")
    print(f"Release ready: {out}  (read README.md before uploading)")
    if args.upload:
        commit = upload(out, args.upload, args.private)
        sha = json.loads((out / "MANIFEST.json").read_text())["files"]["model.safetensors"]
        print(f"Uploaded to https://huggingface.co/{args.upload} (commit {commit}).")
        print("Record it in classifier/published.yaml:\n"
              f'  {args.model}:\n    repo: "{args.upload}"\n    revision: "{commit}"\n    sha256: "{sha}"')


if __name__ == "__main__":
    main()
