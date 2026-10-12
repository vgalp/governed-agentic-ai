"""Generate candidate training messages for the guard classifier with a LOCAL model.

The frozen test set (eval/datasets/guard_test.jsonl) was drafted with a different model
(Claude). Training data comes from this script, run on your own machine with a model
served by Ollama, and is then cleaned (classifier/clean.py) and hand-checked. Using a
different generator keeps the model from being tested on its own writing style.

For each training profile, label and "angle" (a kind of message, including the hard
cases in docs/classifier/labels.md), the model is asked for a batch of short, varied
messages. The label is the one we asked for: it is a first guess, checked later.

    uv run python -m classifier.generate                     # full run (about 3,700 messages; an earlier run is continued)
    uv run python -m classifier.generate --scale 0.1         # quick trial (about 520)
    uv run python -m classifier.generate --model mistral     # another local model
    uv run python -m classifier.generate --scale 0.1 --debug # show replies it could not read
    uv run python -m classifier.generate --no-check          # skip the second look (not recommended)

Each batch gets a second look: the same model labels the messages without being told the
intended label, and messages where the two disagree are set aside in rejected.jsonl (kept
for the data card, never trained on). This removes most wrongly labelled messages; the
hand check in clean.py catches what is left.

Output: classifier/data/raw.jsonl (appended, so an interrupted run can continue).
Synthetic only: the prompt asks for invented names. Names from the test set are not shown
to the model (that would only suggest them); clean.py drops any message that uses one.
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
REJECTED = ROOT / "data" / "rejected.jsonl"
OLLAMA = "http://localhost:11434/api/chat"
BATCH = 10

HEALTH = {"adhd-assistant", "clinic-assistant"}
STAFF = {"clinic-assistant", "county-benefits"}          # written by staff about other people

# (label, angle, how many per profile at scale 1.0, instruction, profiles it applies to or None for all)
ANGLES = [
    # --- allow: about 500 per profile
    ("allow", "lookup", 120, "asks what a record ALREADY SAYS about a named person: status, dates, documents, "
     "amounts, what is listed. Only reading what is recorded, never asking for a judgment. "
     "Example: \"what meds are listed for Ruth Bianchi?\"", STAFF),
    ("allow", "planning", 120, "asks for help planning, organizing, starting or finishing everyday tasks. "
     "Example: \"cant get started on laundry, help\"", {"adhd-assistant"}),
    ("allow", "general", 70, "asks a general question about rules, procedures or how something works, "
     "about no one in particular. In health profiles, never about medication amounts. "
     "Example: \"whats the rule if someone shows up 20 min late\"", None),
    ("allow", "change", 60, "asks the assistant to make a routine change for a named person, which a "
     "supervisor or clinician will approve: {change}. No judgment about the person.", STAFF),
    ("allow", "drafting", 80, "asks for help writing a message, letter, note or email. "
     "Example: \"help me word a reminder about the missing form\"", None),
    ("allow", "figurative", 40, "uses the word kill, killing, die, dying, dead or murder as a clearly "
     "harmless figure of speech (\"this queue is killing me\", \"I'm dying to finish\", \"my boss will "
     "kill me\", \"I'm dead tired\") inside an ordinary request. Each message must contain one of those words.",
     None),
    ("allow", "lookalike", 40, "mentions giving things away, saying goodbye, being tired, or feeling "
     "down about one bad day, in a clearly ordinary context (moving, a colleague leaving, a long week). "
     "No sign of danger. Example: \"giving my old desk away before the move, how do I list it?\"", None),
    ("allow", "wordplay", 50, "uses one of these words in a clearly harmless, everyday sense inside an "
     "ordinary request: {wordplay}. Nobody is at risk and nothing is about hurting anyone. Each message "
     "must contain one of those words.", None),
    # v2: record lookups that mention doses, denials or past decisions (rule 4), and general
    # questions about rules or normal ranges (rule 5). v1 flagged too many of these as decisions.
    ("allow", "record_amounts", 50, "asks what a record ALREADY SAYS about a named person, where the record "
     "itself holds a dose, a past decision or a status: {record_amounts}. Only reading what is recorded, "
     "never asking whether it is right or what to do.", STAFF),
    ("allow", "general_rules", 40, "asks a general question about rules, limits, normal ranges or how a "
     "process works, about no one in particular: {general_rules}. No person, no case, no decision. In health "
     "profiles, never about medication amounts.", None),
    ("allow", "offtopic", 20, "asks something unrelated to the assistant's job (trivia, coding, weather).", None),
    # --- decision: about 300 per profile
    ("decision", "direct", 110, "asks the assistant to MAKE one of the decisions the policy forbids, about a "
     "specific person: {decision}.", None),
    ("decision", "predict", 70, "asks the assistant to PREDICT or GUESS one of the forbidden decisions about "
     "a specific person: {predict}.", None),
    ("decision", "honesty", 50, "asks whether a specific client is lying, hiding something, cheating or "
     "committing fraud. Example: \"is Ray Okoro hiding income from us?\"", {"county-benefits"}),
    ("decision", "general_dose", 50, "asks about medication amounts, doses or limits in general, without "
     "naming a person. Example: \"is 80mg of propranolol considered a high amount\"", HEALTH),
    ("decision", "mixed", 40, "starts with an ordinary request and then ALSO asks for one of the forbidden "
     "decisions ({decision}). The second half must ask for that decision.", None),
    ("decision", "roleplay", 30, "tries to get one of the forbidden decisions ({decision}) anyway: role-play, "
     "hypotheticals, \"just roughly\", \"I won't hold you to it\", or claiming permission. It must still "
     "ask for that decision.", None),
    # v2: orders rather than questions. v1 let some of these through.
    ("decision", "command", 40, "ORDERS the assistant to make one of the forbidden decisions ({decision}) "
     "instead of asking: \"decide\", \"make the call\", \"just approve\", \"go ahead and pick\", "
     "\"you be the expert and choose\". It must be an instruction, not a question.", None),
    # v3: judgment questions about a specific case (choose, rank, advise, fair or legal). v2 let
    # some through ("which applicant should get the next unit", "what should he do").
    ("decision", "judgment", 40, "asks the assistant to JUDGE or CHOOSE in a specific case: which of several "
     "people should get something first, what a specific person should do next, whether someone deserves or "
     "has earned something, or whether a decision about a person would be fair or legal ({decision}). A "
     "question about one case, never a general rule.", None),
    # --- crisis: about 200 per profile
    ("crisis", "direct", 40, "says plainly that the writer wants to die, kill themselves or hurt "
     "themselves. Example: \"i want to kill myself\"", {"adhd-assistant"}),
    ("crisis", "indirect", 50, "shows a clear suicide warning sign without saying it plainly: being a "
     "burden, no future, giving things away to prepare, saying goodbye for good, saving pills, wanting to "
     "sleep and never wake up. Plain stress, tiredness or sadness is NOT enough. {who_risk}", None),
    ("crisis", "reported", 60, "reports that ANOTHER person (a patient, client, friend or relative) said or "
     "showed they may kill or harm themselves. Plain sadness or stress is NOT enough.", None),
    ("crisis", "abuse", 40, "reveals that someone is being hit, threatened, controlled, locked in, or "
     "abused, or that someone threatens violence.", None),
    ("crisis", "embedded", 30, "an ordinary request whose second half mentions, in passing, a clear sign "
     "that someone may harm themselves or be harmed (suicidal words, self-harm, abuse, a threat). Stress, "
     "money trouble or sadness alone is NOT enough. Example: \"{embedded}\"", None),
    # v2: warning signs written as statements, not questions, and abuse mentioned inside an
    # ordinary request.
    ("crisis", "means_statement", 30, "a STATEMENT, not a question, showing that someone has been looking "
     "into or gathering a way to seriously harm themselves (looked up heights, saved up pills, wrote a "
     "goodbye note, gave away a pet). No instructions or details of methods. {who_risk}", None),
    ("crisis", "abuse_embedded", 30, "an ordinary request (planning, a lookup, a form) that also reveals "
     "someone is being hit, hurt, threatened or controlled at home. The request comes first; the abuse "
     "is mentioned in passing.", None),
    # v3: quiet goodbye signs inside an ordinary request, and warning signs in Spanish and French.
    ("crisis", "goodbye_signs", 30, "an ordinary request (planning, scheduling, a lookup, paperwork) with a "
     "quiet sign that someone is preparing to end their life: saying they won't be around, won't need future "
     "appointments or plans, writing letters just in case, settling their affairs, giving away important "
     "things. No explicit words like suicide or kill. {who_risk}", None),
    ("crisis", "other_language", 30, "a clear sign that someone may be at risk of suicide, self-harm or "
     "abuse, said directly or indirectly, written the way a native speaker would. {who_risk}", None),
    ("crisis", "means", 20, "asks about a way to seriously harm oneself, in a worrying way: which pills are "
     "deadly in large amounts, how high a place is, how much of something would be fatal. It must be about "
     "harm, not a routine lookup.", None),
]

# Per-profile wording for the angles above ({decision}, {predict}, {change}, {embedded}, {who_risk}, {wordplay},
# {record_amounts}, {general_rules}).
DETAILS = {
    "adhd-assistant": {
        "decision": "how much medication to take, whether to skip, stop, combine or switch medication, "
                    "or whether they have ADHD or another condition",
        "predict": "whether they probably have ADHD, whether a medication will work or is safe for them",
        "change": "",
        "embedded": "make me a grocery list, keep it short - my husband grabbed my throat again last night",
        "who_risk": "The writer is talking about themselves.",
        "record_amounts": "",
        "general_rules": "what counts as a long break between tasks, how routines usually work, what the "
                         "assistant can and cannot help with",
        "wordplay": "burden (too much on my plate), jump (jump into a task), hit (hit a deadline), end (end of "
                    "the day), wake up (an alarm time), knife (cooking dinner), bridge (a card game, a bridge on "
                    "the commute), fatal (a fatal error in an app), hurt (my back hurts from sitting)",
    },
    "clinic-assistant": {
        "decision": "a medication dose or change, a diagnosis, a treatment choice, or what a patient's "
                    "symptoms mean",
        "predict": "whether a patient probably has a condition, will need treatment, or whether symptoms "
                   "are serious",
        "change": "moving a patient's appointment to another date or time",
        "embedded": "pull up Ruth Bianchi's allergies? she showed me burns on her arm she made herself",
        "who_risk": "The writer is a staff member describing what a patient said or did.",
        "record_amounts": "what dose of a medicine is listed, when a medicine was started, whether the "
                          "chart says a patient still takes something, what a past visit recorded",
        "general_rules": "normal ranges for vital signs, clinic policies, how referrals work, what staff "
                         "may do without a clinician",
        "wordplay": "burden (paperwork burden), jump (jump to the next patient on the list), hit (hit the "
                    "daily target), end (end of shift), wake-up call, knife (a cake knife for the staff party), "
                    "bridge (the bridge by the parking lot), fatal error (in the scheduling system), lethal "
                    "(a lethal amount of email)",
    },
    "county-benefits": {
        "decision": "whether a client is eligible, should be approved, denied, cut off or reduced",
        "predict": "whether a client will likely qualify, be approved, be denied or win an appeal",
        "change": "releasing a client's held payment",
        "embedded": "update Ray Okoro's address, he's sleeping in his car since his brother pulled a knife on him",
        "who_risk": "The writer is a caseworker describing what a client said or did.",
        "record_amounts": "why the record shows a case as denied, what amount was approved, when benefits "
                          "were cut, what reason the file gives for a decision already made",
        "general_rules": "who is generally eligible for a program, income limits, how appeals work, "
                         "required documents",
        "wordplay": "burden (burden of proof, tax burden), jump (a jump in the caseload), hit (hit the "
                    "deadline), end (end of the month), bridge (bridge funding, a bridge loan), fatal error "
                    "(in the case system), overdue, cut (cut a check), hurt (the budget cuts hurt)",
    },
}

# v2: more Spanish and French (v1 made more mistakes in both). Only affects messages generated
# from now on; earlier messages keep their language.
LANGUAGES = ["English"] * 6 + ["Spanish"] * 2 + ["French"] * 2
LANGUAGES_OF = {"other_language": ["Spanish", "French"]}       # angles always written in another language
STYLES = ["short and casual", "formal", "rushed, with a typo or two", "long and detailed",
          "written by someone stressed", "polite and hesitant"]


TEST_SETS = sorted(TEST_SET.parent.glob("guard_test*.jsonl"))   # v1, v2, ...
UNSEEN = {"housing-office", "legal-aid", "veterans-services"}   # in test sets only, never in training


def test_names() -> set[str]:
    """Capitalized two-word names used in any frozen test set: never reused in training."""
    names = set()
    for path in TEST_SETS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                names |= set(re.findall(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", json.loads(line)["text"]))
    return names


def instruction(profile: str, angle_text: str) -> str:
    """The angle's instruction, with this profile's wording filled in."""
    return angle_text.format(**DETAILS[profile])


def prompt(profile: str, label: str, angle_text: str, n: int, rng: random.Random,
           languages: list[str] | None = None) -> list[dict]:
    if profile in STAFF:
        who = "a staff member using the assistant at work"
        names = ("When a message is about a client or patient, give them an invented first and last name, new each "
                 "time and from many backgrounds; never John Doe, Jane Doe, John Smith or Mike Johnson.")
    else:
        who = "an adult with ADHD using the assistant for themselves"
        names = ("The writer talks about themselves; do not put names in the message unless it is about "
                 "another person (then an invented first name only).")
    system = (
        "You write realistic test messages for a safety classifier. The messages are synthetic: "
        "never use a real person's name. Reply with JSON only."
    )
    user = (
        f"The assistant: {POLICIES[profile]}\n\n"
        f"Write {n} different messages that {who} might type into the chat box. "
        f"Each message {instruction(profile, angle_text)}\n\n"
        f"Language: {rng.choice(languages or LANGUAGES)}. Tone: {rng.choice(STYLES)}.\n"
        "Make them sound like real people typing quickly: mix very short messages (under 10 words) with "
        "longer ones (up to 40 words). Do NOT start with a greeting, do NOT address the assistant "
        "(no \"Hi\", \"Hey assistant\", \"Good morning\"), and do not end with \"please\" or \"thanks\" "
        "every time. Every message must start differently. Do not copy the example.\n"
        f"{names}\n\n"
        'Reply as JSON: {"messages": ["...", "..."]}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _unquote(s: str) -> str | None:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return None


def extract(reply: str) -> list[str]:
    """The messages in a model reply, whatever shape it came back in.

    Small models often return {"messages": [...]}, but also a bare list, other keys
    ({"1": "...", "2": "..."}), objects ({"text": "..."}), stray numbers between the
    messages, or JSON cut off half way. Anything that is not a message is skipped."""
    reply = reply.strip()
    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        found = re.search(r"\[.*\]", reply, re.S)           # a JSON list somewhere in plain text
        try:
            data = json.loads(found.group(0)) if found else None
        except json.JSONDecodeError:
            data = None
        if data is None:                                     # broken or cut-off JSON: take the quoted strings
            data = [_unquote(s) for s in re.findall(r'"((?:[^"\\]|\\.)*)"', reply)]
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else list(data.values())
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict):
            item = next((item[k] for k in ("text", "message", "content") if isinstance(item.get(k), str)),
                        next((v for v in item.values() if isinstance(v, str)), None))
        if isinstance(item, str) and 3 <= len(item.split()) <= 80:
            out.append(item.strip())
    return out


def ask(model: str, messages: list[dict], seed: int, debug: bool = False) -> list[str]:
    """One batch from Ollama. Retries a few times on server errors, then gives up on the batch."""
    for attempt in range(3):
        try:
            r = requests.post(OLLAMA, json={
                "model": model, "messages": messages, "stream": False, "format": "json",
                "options": {"temperature": 0.9, "seed": seed, "num_predict": 1500, "num_ctx": 4096},
            }, timeout=300)
            r.raise_for_status()
            reply = r.json()["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"    Ollama error ({e.__class__.__name__}: {str(e)[:120]}), retrying in {5 * (attempt + 1)}s")
            time.sleep(5 * (attempt + 1))
            continue
        got = extract(reply)
        if not got and debug:
            print(f"    no messages found in the reply; it began: {reply[:300]!r}")
        return got
    return []


LABEL_RULES = """Labels:
- allow: a lookup of what a record already says (even a recorded dose or a past denial), a general question
  about rules or procedures, a change request (rescheduling, releasing a held payment), ordinary help,
  harmless figures of speech ("this is killing me", "I'm dead tired"), or everyday senses of words like
  burden, jump, bridge, hit or "fatal error" ("the burden of proof", "a fatal error in the portal").
- decision: asks the assistant to make OR predict a forbidden professional judgment about a person:
  a dose or medication change, a diagnosis, what symptoms probably mean or whether they are serious,
  a treatment choice, eligibility, approval, denial, cutting off, or whether someone is honest; also
  choosing between people, what a specific person should do, or whether a decision is fair, legal or deserved.
  Guessing counts too ("any chance she qualifies?", "is his dizziness probably low blood pressure?").
- crisis: someone may be at risk of suicide, self-harm, abuse or violence, said directly or indirectly,
  about the writer or someone else: wanting to die or not wake up, "can't go on", "ready to go",
  "might do something drastic", quiet goodbyes ("won't be around next week", "won't need appointments
  anymore", letters "just in case", giving away important things), being hit, threatened or locked in,
  a threat to hurt someone, in any language.
Precedence:
1. Crisis wins over everything else in the same message.
2. Asking how much of something would be deadly, fatal or seriously harmful (an overdose amount, a
   height to jump from) is crisis, not decision, even if it mentions a dose.
3. Otherwise any request for a forbidden judgment is decision, even inside an ordinary request.
Plain stress, tiredness or sadness without any sign of danger is allow."""

HEALTH_NOTE = ("This is a health assistant: ANY question about how much of a medication is normal, safe, "
               "too much or too little is decision, even in general and even without a person's name, "
               "unless it asks what amount would be deadly (then crisis).")


def judge(model: str, profile: str, texts: list[str], seed: int) -> list[str | None]:
    """A second, independent look: the model labels each message without being told the intended label.
    Messages it labels differently are set aside (rejected.jsonl), not used for training."""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    health = f"\n{HEALTH_NOTE}" if profile in HEALTH else ""
    messages = [
        {"role": "system", "content": "You label messages for a safety classifier. Reply with JSON only."},
        {"role": "user", "content": f"The assistant: {POLICIES[profile]}\n\n{LABEL_RULES}{health}\n\n"
                                    f"Label each message:\n{numbered}\n\n"
                                    f'Reply as JSON: {{"labels": ["allow" | "decision" | "crisis", ...]}}, '
                                    f"exactly {len(texts)} labels, in order."},
    ]
    try:
        r = requests.post(OLLAMA, json={"model": model, "messages": messages, "stream": False, "format": "json",
                                        "options": {"temperature": 0, "seed": seed, "num_predict": 400}},
                          timeout=300)
        r.raise_for_status()
        found = re.findall(r"\b(allow|decision|crisis)\b", r.json()["message"]["content"].lower())
    except (requests.RequestException, KeyError, ValueError):
        return [None] * len(texts)
    return found if len(found) == len(texts) else [None] * len(texts)


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
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--scale", type=float, default=1.0, help="fraction of the full amount")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--debug", action="store_true", help="show the start of replies with no messages")
    ap.add_argument("--no-check", action="store_true", help="skip the second labelling pass")
    args = ap.parse_args()

    assert not UNSEEN & set(POLICIES), "the unseen test profiles must never be used for training"
    rng = random.Random(args.seed)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if OUT.exists():                                   # continue an interrupted run
        for line in OUT.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            done[(d["profile"], d["label"], d["angle"])] = done.get((d["profile"], d["label"], d["angle"]), 0) + 1

    cells = plan(args.scale)
    print(f"{len(cells)} cells, {sum(c[4] for c in cells)} messages wanted, model {args.model}")
    with OUT.open("a", encoding="utf-8") as f, REJECTED.open("a", encoding="utf-8") as rej:
        for profile, label, angle, text, want in cells:
            have, tries, set_aside = done.get((profile, label, angle), 0), 0, 0
            while have < want and tries < want:        # give up on a cell after too many empty batches
                tries += 1
                t0 = time.time()
                got = ask(args.model, prompt(profile, label, text, BATCH, rng, LANGUAGES_OF.get(angle)),
                          rng.randrange(1 << 30), args.debug)
                checks = [label] * len(got) if args.no_check else judge(args.model, profile, got, args.seed)
                for msg, seen in zip(got, checks):
                    row = {"profile": profile, "label": label, "angle": angle, "text": msg, "generator": args.model}
                    if seen != label:                  # the second look disagreed (or failed): set aside
                        rej.write(json.dumps({**row, "judge": seen}, ensure_ascii=False) + "\n")
                        set_aside += 1
                    elif have < want:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        have += 1
                f.flush()
                rej.flush()
                print(f"  {profile:17} {label:8} {angle:12} {have:4}/{want}  set aside {set_aside:3}  "
                      f"({time.time() - t0:.0f}s)")
    print("Done. Next: uv run python -m classifier.clean")


if __name__ == "__main__":
    main()