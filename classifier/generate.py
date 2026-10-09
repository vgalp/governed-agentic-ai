"""Generate candidate training messages for the guard classifier with a LOCAL model.

The frozen test set (eval/datasets/guard_test.jsonl) was drafted with a different model
(Claude). Training data comes from this script, run on your own machine with a model
served by Ollama, and is then cleaned (classifier/clean.py) and hand-checked. Using a
different generator keeps the model from being tested on its own writing style.

For each training profile, label and "angle" (a kind of message, including the hard
cases in docs/classifier/labels.md), the model is asked for a batch of short, varied
messages. The label is the one we asked for: it is a first guess, checked later.

    uv run python -m classifier.generate                     # full run (about 2,800 messages)
    uv run python -m classifier.generate --scale 0.1         # quick trial (about 520)
    uv run python -m classifier.generate --model qwen2.5:7b  # another local model

Output: classifier/data/raw.jsonl (appended, so an interrupted run can continue).
Synthetic only: the prompt asks for invented names, and names from the test set are
listed as forbidden (clean.py also drops any message that still contains one).
"""

import argparse
import json
import random
import re
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
POLICIES = yaml.safe_load((ROOT / "policies.yaml").read_text(encoding="utf-8"))
TEST_SET = ROOT.parent / "eval" / "datasets" / "guard_test.jsonl"
OUT = ROOT / "data" / "raw.jsonl"
OLLAMA = "http://localhost:11434/api/chat"
BATCH = 10

HEALTH = {"adhd-assistant", "clinic-assistant"}
STAFF = {"clinic-assistant", "county-benefits"}          # written by staff about other people

# (label, angle, how many per profile at scale 1.0, instruction, profiles it applies to or None for all)
ANGLES = [
    # --- allow: about 500 per profile
    ("allow", "lookup", 120, "asks what a record already says about a named person "
     "(status, dates, documents, amounts, what is listed). Only reading, never deciding.", STAFF),
    ("allow", "planning", 120, "asks for help planning, organizing, starting or finishing everyday tasks.",
     {"adhd-assistant"}),
    ("allow", "general", 70, "asks a general question about rules, procedures or how something works, "
     "about no one in particular. In health profiles, never about medication amounts.", None),
    ("allow", "change", 60, "asks to make a routine change that a person will approve: rescheduling an "
     "appointment (clinic) or releasing a held payment (county). No judgment about the person.", STAFF),
    ("allow", "drafting", 80, "asks for help writing a message, letter, note or email.", None),
    ("allow", "figurative", 40, "uses a figure of speech about dying or killing that is clearly not "
     "literal (\"this queue is killing me\", \"I'm dying to finish\") inside an ordinary request.", None),
    ("allow", "lookalike", 40, "mentions giving things away, saying goodbye, being tired, or feeling "
     "down about one bad day, in a clearly ordinary context (moving, a colleague leaving, a long week). "
     "No sign of danger.", None),
    ("allow", "offtopic", 20, "asks something unrelated to the assistant's job (trivia, coding, weather).", None),
    # --- decision: about 300 per profile
    ("decision", "direct", 110, "asks the assistant to make the decision the policy forbids, about a "
     "specific person (a dose or change, a diagnosis, eligibility, approval or denial, cutting off).", None),
    ("decision", "predict", 70, "asks the assistant to predict or guess the outcome of such a decision "
     "(\"will she qualify\", \"does this sound like\", \"what are the chances\").", None),
    ("decision", "honesty", 50, "asks whether a specific person is lying, hiding something, cheating or "
     "committing fraud.", {"county-benefits"}),
    ("decision", "general_dose", 50, "asks about medication amounts or limits in general, without naming "
     "a person (\"what's a normal dose of...\").", HEALTH),
    ("decision", "mixed", 40, "starts with an ordinary request and then also asks for a forbidden decision "
     "in the same message.", None),
    ("decision", "roleplay", 30, "tries to get the decision anyway: role-play, hypotheticals, \"just "
     "roughly\", \"I won't hold you to it\", or claiming permission.", None),
    # --- crisis: about 200 per profile
    ("crisis", "direct", 40, "says plainly that the writer wants to die, kill themselves or hurt "
     "themselves.", {"adhd-assistant"}),
    ("crisis", "indirect", 50, "shows suicide warning signs without saying it plainly: being a burden, no "
     "future, giving things away to prepare, saying goodbye for good, saving pills, wanting to sleep forever.",
     None),
    ("crisis", "reported", 60, "reports that ANOTHER person (a patient, client, friend or relative) said or "
     "showed they may harm themselves.", None),
    ("crisis", "abuse", 40, "reveals that someone is being hit, threatened, controlled, locked in, or "
     "abused, or that someone threatens violence.", None),
    ("crisis", "embedded", 30, "an ordinary request that also contains a sign of crisis (the crisis is "
     "the second half, mentioned in passing).", None),
    ("crisis", "means", 20, "asks about means of self-harm in a worrying way (which pills are dangerous "
     "in large amounts, highest places), possibly framed innocently.", None),
]

LANGUAGES = ["English"] * 8 + ["Spanish", "French"]
STYLES = ["short and casual", "formal", "rushed, with a typo or two", "long and detailed",
          "written by someone stressed", "polite and hesitant"]


def test_names() -> set[str]:
    """Capitalized two-word names used in the frozen test set: never reused in training."""
    names = set()
    for line in TEST_SET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            names |= set(re.findall(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", json.loads(line)["text"]))
    return names


def prompt(profile: str, label: str, angle_text: str, n: int, forbidden: list[str], rng: random.Random) -> list[dict]:
    who = ("a staff member using the assistant at work" if profile in STAFF
           else "an adult with ADHD using the assistant for themselves")
    system = (
        "You write realistic test messages for a safety classifier. The messages are synthetic: "
        "invent every name, and never use a real person's name. Reply with JSON only."
    )
    user = (
        f"The assistant: {POLICIES[profile]}\n\n"
        f"Write {n} different messages that {who} might type. Each message {angle_text}\n\n"
        f"Language: {rng.choice(LANGUAGES)}. Style: {rng.choice(STYLES)}. Vary length (5 to 40 words), "
        "wording and situation; do not start them the same way. Invent new first and last names. "
        f"Do not use these names: {', '.join(forbidden)}.\n\n"
        'Reply as JSON: {"messages": ["...", "..."]}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ask(model: str, messages: list[dict], seed: int) -> list[str]:
    r = requests.post(OLLAMA, json={"model": model, "messages": messages, "stream": False, "format": "json",
                                    "options": {"temperature": 0.9, "seed": seed}}, timeout=300)
    r.raise_for_status()
    try:
        out = json.loads(r.json()["message"]["content"]).get("messages", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [m.strip() for m in out if isinstance(m, str) and 3 <= len(m.split()) <= 80]


def plan(scale: float) -> list[tuple[str, str, str, str, int]]:
    """(profile, label, angle, instruction, how many) for every cell that applies."""
    cells = []
    for profile in POLICIES:
        for label, angle, count, text, only in ANGLES:
            if only is None or profile in only:
                cells.append((profile, label, angle, text, max(BATCH, round(count * scale))))
    return cells


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="mistral")
    ap.add_argument("--scale", type=float, default=1.0, help="fraction of the full amount")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    assert "housing-office" not in POLICIES, "the unseen test profile must never be used for training"
    rng = random.Random(args.seed)
    forbidden = sorted(test_names())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if OUT.exists():                                   # continue an interrupted run
        for line in OUT.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            done[(d["profile"], d["label"], d["angle"])] = done.get((d["profile"], d["label"], d["angle"]), 0) + 1

    cells = plan(args.scale)
    print(f"{len(cells)} cells, {sum(c[4] for c in cells)} messages wanted, model {args.model}")
    with OUT.open("a", encoding="utf-8") as f:
        for profile, label, angle, text, want in cells:
            have, tries = done.get((profile, label, angle), 0), 0
            while have < want and tries < want:        # give up on a cell after too many empty batches
                tries += 1
                t0 = time.time()
                got = ask(args.model, prompt(profile, label, text, BATCH, rng.sample(forbidden, 12), rng),
                          rng.randrange(1 << 30))
                for msg in got[: want - have]:
                    f.write(json.dumps({"profile": profile, "label": label, "angle": angle, "text": msg,
                                        "generator": args.model}, ensure_ascii=False) + "\n")
                    have += 1
                f.flush()
                print(f"  {profile:17} {label:8} {angle:12} {have:4}/{want}  ({time.time() - t0:.0f}s)")
    print("Done. Next: uv run python -m classifier.clean")


if __name__ == "__main__":
    main()