"""Every system scored on the crisis benchmark, side by side (no new runs: reads eval/results/).

    uv run python -m eval.compare_crisis_benchmark                       # clinic-assistant, education-staff
    uv run python -m eval.compare_crisis_benchmark --policies all
    uv run python -m eval.compare_crisis_benchmark --versions --reference guard-modernbert-v6.1 --policies all
    uv run python -m eval.compare_crisis_benchmark --benchmark v2 --versions   # v6.1 vs v6 and v4, primary sources

Benchmark v2 (`--benchmark v2`): the reference defaults to guard-modernbert-v6.1, and only the primary,
human-labelled sources are compared unless `--sources all` (files then end in _all_sources). Texts of
WildGuardTest messages are never written to a result file (they are not redistributed): those are
listed by id only.

Two comparisons, each in its own file:
- **Guards** (default): the other guard models against one guard classifier, as fixed in the protocol
  (`--reference`, default guard-modernbert-v4, the classifier the protocol was written for). Other
  versions of the classifier are left out, so later releases never change this file's tests.
  Writes eval/results/crisis_benchmark_v1_comparison.md.
- **Versions** (`--versions`): the guard classifier's own releases, each against `--reference`.
  Writes eval/results/crisis_benchmark_v1_versions.md.

Only the policies every compared result has in common are compared, so each system answers the
same messages. Each file (commit it) has a table of scores, the paired tests, and which crisis
messages each system missed.

Paired tests (fixed in the protocol before the other guards were run): each system against the
reference (the guard classifier), one policy at a time so each message counts once, on two
questions: crisis messages caught, and safe messages given the crisis reply. McNemar's exact test
uses only the messages where exactly one of the two systems is right. All p-values in the file are
adjusted together with Holm's method; a difference is called significant at 0.05 after adjustment.
"""

import argparse
import json
import math
from collections import defaultdict

from eval import build_crisis_benchmark as v1
from eval import build_crisis_benchmark_v2 as v2
from eval.run_crisis_benchmark import pct, rate
from eval.run_guard_test import RESULTS

DEFAULT_POLICIES = ("clinic-assistant", "education-staff")
REFERENCE = "classifier"           # the system everything is tested against
PROTOCOL_MODEL = "guard-modernbert-v4"   # the classifier the v1 guard comparison was fixed for
BENCHMARKS = {
    "v1": {"module": v1, "reference": PROTOCOL_MODEL, "primary": None, "hard": ("xstest", "XSTest safe prompts")},
    "v2": {"module": v2, "reference": "guard-modernbert-v6.1", "primary": v2.PRIMARY,
           "hard": ("orbench", "OR-Bench-Hard self-harm prompts")},
}
NOT_QUOTED = {"wildguard"}       # sources whose texts may not be redistributed: ids only
ALPHA = 0.05


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: b and c are the messages only the first, or only the second,
    system got right. Under no difference, each disagreement is a fair coin."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def holm(pvalues: list[float]) -> list[float]:
    """Holm-adjusted p-values, in the original order."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted, running = [0.0] * len(pvalues), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def paired_tests(runs: list[dict], policies: list[str], reference: str = REFERENCE) -> list[dict]:
    """For each other system, policy and question: who got which messages right, and McNemar's p."""
    ref = find_reference(runs, reference)
    if ref is None:
        return []
    def right(d, policy, expected):
        return {r["id"]: (r["predicted"] == "crisis") == (expected == "crisis")
                for r in d["rows"] if r["policy"] == policy and r["expected"] == expected}
    tests = []
    for d in runs:
        if d is ref:
            continue
        for policy in policies:
            for expected, question in (("crisis", "crisis caught"), ("not_crisis", "no false crisis")):
                a, o = right(ref, policy, expected), right(d, policy, expected)
                ids = a.keys() & o.keys()
                b = sum(a[i] and not o[i] for i in ids)          # only the reference right
                c = sum(o[i] and not a[i] for i in ids)          # only the other system right
                tests.append({"system": d["name"], "policy": policy, "question": question, "n": len(ids),
                              "only_reference": b, "only_other": c, "p": mcnemar_exact(b, c)})
    for t, adj in zip(tests, holm([t["p"] for t in tests])):
        t["p_holm"] = adj
        t["significant"] = adj < ALPHA
        t["better"] = None if not t["significant"] else (reference if t["only_reference"] > t["only_other"]
                                                         else t["system"])
    return tests


def find_reference(runs: list[dict], reference: str) -> dict | None:
    """The run named `reference`, or the only run of that system. Never a guess between several."""
    named = [d for d in runs if d["name"] == reference]
    if named:
        return named[0]
    of_system = [d for d in runs if d["system"] == reference]
    if len(of_system) > 1:
        raise ValueError(f"{len(of_system)} runs of {reference}: name the reference, e.g. "
                         f"{of_system[0]['name']!r}")
    return of_system[0] if of_system else None


def classifier_name(model: str) -> str:
    return f"{REFERENCE} ({model})"


def select(runs: list[dict], versions: bool, reference_model: str) -> list[dict]:
    """Guards: every other guard and one classifier run. Versions: every classifier run."""
    if versions:
        return [d for d in runs if d["system"] == REFERENCE]
    return [d for d in runs if d["system"] != REFERENCE or d.get("model") == reference_model]


def load_runs(version: str = "v1") -> list[dict]:
    runs = []
    for path in sorted(RESULTS.glob(f"crisis_benchmark_{version}_*.json")):
        d = json.loads(path.read_text())
        if "rows" in d and "summary" in d:
            d["file"] = path.name
            d["name"] = d["system"] + (f" ({d['model']})" if d.get("model") else "")
            runs.append(d)
    return runs


def only_sources(runs: list[dict], sources: tuple[str, ...] | None) -> list[dict]:
    """The runs with their rows limited to these sources (None: all)."""
    if not sources:
        return runs
    return [{**d, "rows": [r for r in d["rows"] if r["source"] in sources]} for d in runs]


def quotable(items: list[dict]) -> dict[str, str]:
    """id -> text for the messages a result file may quote."""
    return {it["id"]: it["text"] for it in items if it.get("source") not in NOT_QUOTED}


def compare(runs: list[dict], policies: list[str], hard_source: str = "xstest") -> tuple[list[dict], dict]:
    """Scores per system on the chosen policies, and for each crisis message how many of those
    policies each system missed it under."""
    scores, missed = [], defaultdict(dict)
    for d in runs:
        rows = [r for r in d["rows"] if r["policy"] in policies]
        crisis = [r for r in rows if r["expected"] == "crisis"]
        other = [r for r in rows if r["expected"] == "not_crisis"]
        xs = [r for r in other if r["source"] == hard_source]
        scores.append({
            "name": d["name"],
            "caught": rate(sum(r["predicted"] == "crisis" for r in crisis), len(crisis)),
            "false_crisis": rate(sum(r["predicted"] == "crisis" for r in other), len(other)),
            "xstest_false": rate(sum(r["predicted"] == "crisis" for r in xs), len(xs)),
        })
        for r in crisis:
            if r["predicted"] != "crisis":
                missed[r["id"]][d["name"]] = missed[r["id"]].get(d["name"], 0) + 1
    return scores, missed


def tests_markdown(tests: list[dict], reference_name: str) -> list[str]:
    if not tests:
        return []
    out = ["", f"## Paired tests against {reference_name}", "",
           "McNemar's exact test, per policy (each message once). \"Only ref.\" / \"Only other\": messages "
           "only that system got right. p adjusted with Holm's method across every test in this table; "
           f"significant at {ALPHA}.", "",
           "| System | Policy | Question | Messages | Only ref. | Only other | p (Holm) | Result |",
           "|---|---|---|---|---|---|---|---|"]
    for t in tests:
        result = (f"**{t['better']} better**" if t["significant"] else "no significant difference")
        out.append(f"| {t['system']} | {t['policy']} | {t['question']} | {t['n']} | {t['only_reference']} | "
                   f"{t['only_other']} | {t['p_holm']:.3g} | {result} |")
    return out


def markdown(scores: list[dict], missed: dict, policies: list[str], texts: dict[str, str], names: list[str],
             tests: list[dict] | None = None, reference_name: str = REFERENCE,
             title: str = "all systems compared", version: str = "v1", hard_label: str = "XSTest safe prompts",
             sources_note: str = "") -> str:
    out = [f"# Crisis benchmark {version}: {title}\n",
           f"Policies compared: {', '.join(policies)} (each message labelled once per policy).{sources_note}\n",
           f"| System | Crisis messages caught | Safe messages given the crisis reply | {hard_label} given the crisis reply |",
           "|---|---|---|---|"]
    for s in sorted(scores, key=lambda s: -(s["caught"]["rate"] or 0)):
        out.append(f"| {s['name']} | {pct(s['caught'])} | {pct(s['false_crisis'])} | {pct(s['xstest_false'])} |")
    out += tests_markdown(tests or [], reference_name)
    out += ["", f"## Crisis messages missed (number of the {len(policies)} policies each system missed it under)", "",
            "| Message | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for i in sorted(missed, key=lambda i: -len(missed[i])):
        text = texts.get(i, "")[:90].replace("|", "/").replace("\n", " ")
        out.append(f"| `{i[:22]}` {text} | " + " | ".join(str(missed[i].get(n, "")) for n in names) + " |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policies", default=",".join(DEFAULT_POLICIES), help="comma-separated, or 'all'")
    ap.add_argument("--versions", action="store_true", help="compare the guard classifier's own releases")
    ap.add_argument("--benchmark", choices=list(BENCHMARKS), default="v1")
    ap.add_argument("--reference", default=None,
                    help="the classifier model tested against (default: v4 for v1, v6.1 for v2)")
    ap.add_argument("--sources", default=None, help="v2: comma-separated sources, or 'all' (default: primary)")
    args = ap.parse_args()
    bench = BENCHMARKS[args.benchmark]
    reference = args.reference or bench["reference"]
    ref_name = classifier_name(reference)
    sources = (None if args.sources == "all" else tuple(args.sources.split(",")) if args.sources
               else bench["primary"])
    runs = only_sources(select(load_runs(args.benchmark), args.versions, reference), sources)
    if not any(d["name"] == ref_name for d in runs):
        raise SystemExit(f"No benchmark result for {ref_name}.")
    common = set.intersection(*({r["policy"] for r in d["rows"]} for d in runs)) if runs else set()
    policies = sorted(common) if args.policies == "all" else [p for p in args.policies.split(",") if p in common]
    if not policies:
        raise SystemExit(f"No policy is shared by every result. Shared: {sorted(common) or 'none'}")
    path = bench["module"].BENCHMARK
    texts = quotable([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]) if path.exists() else {}
    hard_source, hard_label = bench["hard"]
    scores, missed = compare(runs, policies, hard_source)
    tests = paired_tests(runs, policies, ref_name)
    note = f" Sources: {', '.join(sources)}." if sources else (" Sources: all." if bench["primary"] else "")
    md = markdown(scores, missed, policies, texts, [d["name"] for d in runs], tests, ref_name,
                  title="the guard classifier's releases compared" if args.versions else "all systems compared",
                  version=args.benchmark, hard_label=hard_label, sources_note=note)
    suffix = "_all_sources" if bench["primary"] and sources is None else ""
    kind = "versions" if args.versions else "comparison"
    out = RESULTS / f"crisis_benchmark_{args.benchmark}_{kind}{suffix}.md"
    out.write_text(md)
    print(md)
    print(f"Saved {out.relative_to(RESULTS.parent.parent)}.")


if __name__ == "__main__":
    main()
