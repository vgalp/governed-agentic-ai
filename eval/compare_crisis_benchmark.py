"""Every system scored on the crisis benchmark, side by side (no new runs: reads eval/results/).

    uv run python -m eval.compare_crisis_benchmark                       # clinic-assistant, education-staff
    uv run python -m eval.compare_crisis_benchmark --policies all

Only the policies every result has in common are compared, so each system answers the same
messages. Writes eval/results/crisis_benchmark_v1_comparison.md (commit it): a table of scores, the
paired tests, and which crisis messages each system missed.

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

from eval.build_crisis_benchmark import BENCHMARK
from eval.run_crisis_benchmark import pct, rate
from eval.run_guard_test import RESULTS

DEFAULT_POLICIES = ("clinic-assistant", "education-staff")
REFERENCE = "classifier"           # the system everything is tested against
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
    ref = next((d for d in runs if d["system"] == reference), None)
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


def load_runs() -> list[dict]:
    runs = []
    for path in sorted(RESULTS.glob("crisis_benchmark_v1_*.json")):
        d = json.loads(path.read_text())
        if "rows" in d and "summary" in d:
            d["file"] = path.name
            d["name"] = d["system"] + (f" ({d['model']})" if d.get("model") else "")
            runs.append(d)
    return runs


def compare(runs: list[dict], policies: list[str]) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """Scores per system on the chosen policies, and for each crisis message how many of those
    policies each system missed it under."""
    scores, missed = [], defaultdict(dict)
    for d in runs:
        rows = [r for r in d["rows"] if r["policy"] in policies]
        crisis = [r for r in rows if r["expected"] == "crisis"]
        other = [r for r in rows if r["expected"] == "not_crisis"]
        xs = [r for r in other if r["source"] == "xstest"]
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
             tests: list[dict] | None = None, reference_name: str = REFERENCE) -> str:
    out = [f"# Crisis benchmark v1: all systems compared\n",
           f"Policies compared: {', '.join(policies)} (each message labelled once per policy).\n",
           "| System | Crisis messages caught | Safe messages given the crisis reply | XSTest safe prompts given the crisis reply |",
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
    args = ap.parse_args()
    runs = load_runs()
    common = set.intersection(*({r["policy"] for r in d["rows"]} for d in runs)) if runs else set()
    policies = sorted(common) if args.policies == "all" else [p for p in args.policies.split(",") if p in common]
    if not policies:
        raise SystemExit(f"No policy is shared by every result. Shared: {sorted(common) or 'none'}")
    texts = {json.loads(line)["id"]: json.loads(line)["text"]
             for line in BENCHMARK.read_text(encoding="utf-8").splitlines() if line.strip()}
    scores, missed = compare(runs, policies)
    tests = paired_tests(runs, policies)
    ref_name = next((d["name"] for d in runs if d["system"] == REFERENCE), REFERENCE)
    md = markdown(scores, missed, policies, texts, [d["name"] for d in runs], tests, ref_name)
    out = RESULTS / "crisis_benchmark_v1_comparison.md"
    out.write_text(md)
    print(md)
    print(f"Saved {out.relative_to(RESULTS.parent.parent)}.")


if __name__ == "__main__":
    main()
