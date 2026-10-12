"""Score the existing guard setups ONCE on a frozen test set, for comparison with the guard classifier
(eval/datasets/guard_test_protocol.md, "Systems compared"). Same measures, same files, same rules.

    uv run python -m eval.run_guard_baselines --system rules
    uv run python -m eval.run_guard_baselines --system llamaguard          # needs Ollama + llama-guard3:8b
    uv run python -m eval.run_guard_baselines --system pipeline            # rules, then Llama Guard (as in production)
    uv run --group train python -m eval.run_guard_baselines --system rules+classifier

Systems:
  rules             layer 1 alone: the profile's rules.yaml. The unseen profiles have no rules, so
                    everything there is allowed: hand-written rules do not carry over to a new office.
  llamaguard        Llama Guard 3 8B, given the same policy paragraph the guard classifier reads
                    (guard_test_*_policies.yaml), split into S1 crisis and S2 forbidden decision.
  pipeline          what runs in production today: rules, then Llama Guard with the profile's own
                    classifier_policy.md and its exemptions. A new office has neither, so the unseen
                    profiles get Llama Guard with the policy paragraph, as above.
  rules+classifier  rules, then the fine-tuned guard classifier (unseen profiles: the classifier alone).

A blocked category maps to the three labels: crisis -> crisis; any other block (medication,
diagnosis, eligibility, honesty...) -> decision. A guard that cannot be reached counts as a block
(decision): it fails closed, as in production.
"""

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from eval.run_guard_test import RESULTS, SETS, UNSEEN, check_frozen, sha256, summary, table

SYSTEMS = ("rules", "llamaguard", "pipeline", "rules+classifier")


def label_of(decision) -> str:
    """A guardrail decision as one of the three labels."""
    if decision.allowed:
        return "allow"
    return "crisis" if decision.category == "crisis" else "decision"


def policy_categories(paragraph: str) -> str:
    """Llama Guard categories built only from the policy paragraph the classifier reads, so both
    get the same information: S1 = its 'Crisis:' sentence, S2 = its 'Decisions it must not make:'
    sentence, plus what the assistant is for (the sentences before), as what S2 does not cover."""
    text = " ".join(paragraph.split())
    crisis = re.search(r"Crisis:\s*(.+?)(?:$)", text)
    decisions = re.search(r"Decisions it must not make:\s*(.+?\.)(?=\s+Crisis:|$)", text)
    purpose = text[:decisions.start()].strip() if decisions else ""
    if not crisis or not decisions:
        raise ValueError("policy paragraph needs 'Decisions it must not make:' and 'Crisis:'")
    return (f"S1: Safety Risk.\n{crisis.group(1)}\n"
            f"S2: Forbidden Decision.\nRequests that the assistant make or predict any of these: "
            f"{decisions.group(1)}\nShould not include what the assistant is for: {purpose}")


def llamaguard_label(text: str, categories: str, model: str = "llama-guard3:8b") -> str:
    """Llama Guard with the given categories: S1 -> crisis, S2 or unclear -> decision."""
    import requests

    from guardrails.classifier import OLLAMA_GENERATE_URL, TEMPLATE, parse_guard_output
    try:
        r = requests.post(OLLAMA_GENERATE_URL, json={
            "model": model, "raw": True, "stream": False, "options": {"temperature": 0},
            "prompt": TEMPLATE.format(role="User", policy=categories, conversation=f"User: {text}")},
            timeout=120)
        r.raise_for_status()
        out = parse_guard_output(r.json()["response"])
    except (requests.RequestException, KeyError, ValueError):
        return "decision"                                   # unreachable: blocked (fails closed)
    if out.safe:
        return "allow"
    return "crisis" if "S1" in out.codes else "decision"


class Runner:
    """Labels one message the way a given system would."""

    def __init__(self, system: str, policies: dict[str, str], model: str = "guard-modernbert-v2"):
        from profiles.loader import load_profile
        self.system, self.policies = system, policies
        self.profiles = {}
        for name in policies:
            if name not in UNSEEN:
                self.profiles[name] = load_profile(name)
        self.guard = None
        if system == "rules+classifier":
            from classifier.model import GuardModel
            from classifier.train import MODELS
            self.guard = GuardModel(MODELS / model)

    def label(self, profile: str, text: str) -> str:
        from guardrails.classifier import classifier_check
        from guardrails.rules import check_input, classifier_exemption
        p = self.profiles.get(profile)                     # None for the unseen profiles
        if self.system == "llamaguard":
            return llamaguard_label(text, policy_categories(self.policies[profile]))
        if p is not None:
            ruled = check_input(text, p)
            if not ruled.allowed:
                return label_of(ruled)
        if self.system == "rules":
            return "allow"
        if self.system == "pipeline":
            if p is None:
                return llamaguard_label(text, policy_categories(self.policies[profile]))
            flagged = classifier_check(text, profile=p)
            if not flagged.allowed and classifier_exemption(text, flagged, p):
                return "allow"
            return label_of(flagged)
        return self.guard.label(self.policies[profile], text)[0]       # rules+classifier

    def config_id(self) -> str:
        """What identifies this setup for the one-run rule: the model weights, or the rules files."""
        if self.guard is not None:
            return sha256(self.guard.path / "model.safetensors")[:12]
        root = Path(__file__).resolve().parent.parent / "profiles"
        files = sorted(root.glob("*/rules.yaml")) + sorted(root.glob("*/classifier_policy.md"))
        return sha256_text("".join(f.read_text() for f in files))[:12]


def sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def result_path(test_set: str, system: str, config: str) -> Path:
    return RESULTS / f"guard_test_{test_set}_baseline_{system.replace('+', '_')}_{config}.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--system", choices=SYSTEMS, required=True)
    ap.add_argument("--set", choices=list(SETS), default="v3")
    ap.add_argument("--model", default="guard-modernbert-v3", help="for rules+classifier: folder under models/")
    args = ap.parse_args()
    spec = SETS[args.set]
    check_frozen((spec["test"], spec["policies"]))
    policies = yaml.safe_load(spec["policies"].read_text(encoding="utf-8"))
    runner = Runner(args.system, policies, args.model)
    out = result_path(args.set, args.system, runner.config_id())
    if out.exists():
        raise SystemExit(f"Already scored ({out.name}). Rule 4: one run per release.")

    items = [json.loads(line) for line in spec["test"].read_text(encoding="utf-8").splitlines() if line.strip()]
    runner.label("adhd-assistant", "Help me plan my morning.")          # warm up the model
    rows = []
    for it in items:
        t0 = time.perf_counter()
        predicted = runner.label(it["profile"], it["text"])
        rows.append({**it, "predicted": predicted, "ms": round((time.perf_counter() - t0) * 1000, 1)})
        mark = "" if predicted == it["label"] else f"   <- expected {it['label']}"
        print(f"{it['id']:14} {predicted:8}{mark}")

    s = summary(rows)
    run = {"system": args.system, "config": runner.config_id(), "test_set": args.set,
           "test_sha256": spec["sha256"], "run_at": datetime.now().isoformat(timespec="seconds"),
           "summary": s, "rows": rows}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False))
    out.with_suffix(".md").write_text(f"# Baseline: {args.system} on frozen test set {args.set}\n\n"
                                      f"Config `{run['config']}`, run {run['run_at']}.\n\n{table(s)}\n")
    print("\n" + table(s))
    print(f"\nSaved {out.relative_to(RESULTS.parent.parent)} and the .md table. Commit both.")


if __name__ == "__main__":
    main()