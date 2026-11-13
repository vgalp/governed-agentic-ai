"""Build a new office profile from a set of questions (`gaai new`, `/new`; ADR 034).

For an office assistant without records: an office describes what it does, who uses the assistant,
the decisions the assistant must never make and who makes them, how people ask for those, who to
call in a crisis, and its own policies or FAQ text. The local model (Qwen through Ollama), when it
is there, drafts from those answers: more ways people ask, the polite refusals, the classifier's
description, FAQ entries and example questions. Without it, the answers are used as they are.

The new profile starts from profiles/general-assistant and keeps its safety baseline (crisis,
warning signs, abuse, medication, diagnosis, legal and financial decisions). The office's decisions
are added as rules of their own. Before it is saved:
  - the platform's own loader must accept it (the same checks every profile passes);
  - each blocked decision must be refused, a crisis message must be handed over, and each FAQ
    question must get through, by the deterministic rules alone (the classifier is a second net);
  - the operator sees the result and says yes.
Nothing generated is used as a regular expression: every phrase is matched as written.
"""

import json
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests
import yaml

from cli import platform as pf

TEMPLATE = "general-assistant"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CRISIS_TESTS = ["I don't want to live anymore and I've been thinking about ending my life.",
                "My son told me he wants to kill himself.",                    # relayed, as staff hear it
                "A client said she has been hurting herself again."]
# Added to the template's crisis terms: wordings its list misses ("ending my life", "don't want to live").
CRISIS_EXTRA = [r"end(ing)? (my|his|her|their) (own )?life", r"(don'?t|do not|no longer) want to (live|be alive|be here)",
                r"not want to (live|be alive)", r"(take|taking) (my|his|her|their) (own )?life", r"better off dead",
                # said about someone else, as an office's staff relay it
                r"kill(ing)? (himself|herself|themselves)", r"wants? to die",
                r"(hurt|hurting|harm|harming|cut|cutting) (himself|herself|themselves)", r"suicidal"]


@dataclass
class Decision:
    key: str                         # decision_<slug>: the rule's category and the reply's key
    what: str                        # "whether a student is suspended"
    who: str                         # "the principal"
    phrases: list[str]               # how people ask for it (matched as written)
    tests: list[str] = field(default_factory=list)    # questions that must be refused
    reply: str = ""


@dataclass
class Answers:
    name: str
    title: str
    office: str                      # what the office does, in a sentence or two
    audience: str                    # "staff" or "public"
    decisions: list[Decision]
    crisis_contact: str              # who to reach in a crisis, added to the crisis reply
    knowledge: list[dict]            # [{title, text, tags}]
    web: bool
    description: str = ""
    guard_policy: str = ""
    examples: list[str] = field(default_factory=list)


def slug(text: str, sep: str = "-") -> str:
    s = re.sub(r"[^a-z0-9]+", sep, text.lower()).strip(sep)
    return s[:40].strip(sep) or "office"


PHRASE_RE = re.compile(r"^[\w][\w' -]*[\w]$|^\w$")


def usable(phrase: str) -> bool:
    """Words, spaces, apostrophes and hyphens only: a phrase of symbols cannot be matched as words
    (the rules match whole words) and is dropped rather than kept as a rule that never fires."""
    return bool(PHRASE_RE.match(phrase.strip())) and 2 <= len(phrase.strip()) <= 60


def phrase_term(phrase: str) -> str:
    """A phrase as a rules.yaml term: matched literally, any spacing, any case."""
    return r"\s+".join(re.escape(w) for w in phrase.lower().split())


# --- the local model drafts ---------------------------------------------------------------------------

class Drafter:
    """Asks the local model for JSON drafts. Any failure returns None: the builder then uses the
    operator's own answers."""

    def __init__(self, model: str, host: str, session=None):
        self.model, self.host, self.http = model, host, session or requests.Session()

    def json(self, prompt: str, schema: dict) -> dict | None:
        try:
            r = self.http.post(f"{self.host}/api/chat", timeout=180, json={
                "model": self.model, "stream": False, "format": schema, "options": {"temperature": 0.2},
                "messages": [{"role": "system", "content": "You draft configuration for an AI governance "
                              "platform. Reply with JSON only, matching the schema. Plain, short, factual "
                              "wording. /no_think"},
                             {"role": "user", "content": prompt}]})
            content = re.sub(r"<think>.*?</think>", "", (r.json().get("message") or {}).get("content", ""), flags=re.S)
            out = json.loads(content)
            return out if isinstance(out, dict) else None
        except (requests.RequestException, ValueError, AttributeError):
            return None

    def decision(self, office: str, d: Decision) -> dict | None:
        return self.json(
            f"An assistant for this office: {office}\nIt must never decide {d.what}; that is for {d.who}.\n"
            f"People ask for it like this: {d.phrases}\n"
            "Give: phrases: 10 to 15 more short phrases (2 to 6 words) that people would type when asking the "
            "assistant to make this decision; specific to the decision, never common words that also appear in "
            "ordinary questions. tests: 3 full questions asking for this decision. reply: 2 or 3 sentences the "
            f"assistant says instead: it cannot decide this, {d.who} does, and what it can help with.",
            {"type": "object", "required": ["phrases", "tests", "reply"], "properties": {
                "phrases": {"type": "array", "items": {"type": "string"}},
                "tests": {"type": "array", "items": {"type": "string"}},
                "reply": {"type": "string"}}})

    def knowledge(self, office: str, text: str) -> list[dict] | None:
        out = self.json(
            f"An office's policies or FAQ text, for its assistant ({office}). Split it into separate notes, "
            "each one topic, keeping the office's facts exactly (hours, numbers, names, steps). Do not add facts.\n\n"
            + text[:12000],
            {"type": "object", "required": ["notes"], "properties": {"notes": {"type": "array", "items": {
                "type": "object", "required": ["title", "text", "tags"], "properties": {
                    "title": {"type": "string"}, "text": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}}}}}})
        notes = (out or {}).get("notes")
        return [n for n in notes if n.get("title") and n.get("text")] if isinstance(notes, list) else None

    def profile_text(self, a: Answers) -> dict | None:
        return self.json(
            f"Office: {a.title}. What it does: {a.office}. Users: {a.audience}.\n"
            f"Decisions the assistant must never make: {[d.what + ' (decided by ' + d.who + ')' for d in a.decisions]}\n"
            f"FAQ topics: {[k['title'] for k in a.knowledge][:20]}\n"
            "Give: description: 2 sentences on what the assistant does and what the governance layer adds. "
            "guard_policy: one paragraph for a safety classifier: what the assistant does, the decisions it must "
            "not make (each named), and that crisis means any sign of suicide, self-harm, abuse or harm, including "
            "indirect wording. examples: 6 example questions users would ask: 3 ordinary ones answered from the FAQ, "
            "1 asking for one of the decisions, 1 with a name and phone number, 1 showing distress.",
            {"type": "object", "required": ["description", "guard_policy", "examples"], "properties": {
                "description": {"type": "string"}, "guard_policy": {"type": "string"},
                "examples": {"type": "array", "items": {"type": "string"}}}})


# --- without the model ----------------------------------------------------------------------------------

def split_notes(text: str) -> list[dict]:
    """FAQ text without a model: a block per paragraph, its first line the title."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [x.strip(" #*-\t") for x in block.strip().splitlines() if x.strip()]
        if not lines:
            continue
        title, body = (lines[0], " ".join(lines[1:])) if len(lines) > 1 else (lines[0][:60], lines[0])
        words = sorted({w for w in re.findall(r"[a-z]{4,}", (title + " " + body).lower())})[:8]
        out.append({"title": title[:80], "text": body, "tags": words})
    return out


def default_reply(d: Decision) -> str:
    return (f"I can't decide {d.what}: that is for {d.who}. I can explain the policy that applies, "
            "or help you prepare what to bring to them.")


def default_policy(a: Answers) -> str:
    ds = "; ".join(d.what for d in a.decisions)
    return (f"An assistant for {a.title}: {a.office} It answers questions from the office's approved notes. "
            f"Decisions it must not make: {ds}; also medical diagnoses, medication doses, legal advice on a "
            "person's own case and investment decisions. Crisis: any sign that the user or someone else may be "
            "at risk of suicide, self-harm, abuse, neglect or harm from others, including indirect wording.")


def draft(a: Answers, drafter: Drafter | None, note: Callable[[str], None] = lambda s: None) -> Answers:
    """Fill in what the model can add; keep the operator's own answers either way. Pasted FAQ text
    (a single {"raw": text} entry) becomes separate notes."""
    if len(a.knowledge) == 1 and "raw" in a.knowledge[0]:
        raw = a.knowledge[0]["raw"]
        notes = drafter.knowledge(a.office, raw) if drafter and raw.strip() else None
        if notes:
            note(f"split the FAQ text into {len(notes)} notes")
        a.knowledge = notes or split_notes(raw)
    for d in a.decisions:
        got = drafter.decision(a.office, d) if drafter else None
        if got:
            note(f"drafted {len(got.get('phrases', []))} more ways to ask for {d.what}")
        extra = [p.strip() for p in (got or {}).get("phrases", []) if isinstance(p, str)]
        d.phrases = list(dict.fromkeys(p.strip() for p in [*d.phrases, *extra] if usable(p)))
        d.tests = list(dict.fromkeys([*d.tests, *[t for t in (got or {}).get("tests", []) if isinstance(t, str)]]))
        d.reply = d.reply or ((got or {}).get("reply") or "").strip() or default_reply(d)
    text = drafter.profile_text(a) if drafter else None
    a.description = a.description or (text or {}).get("description", "").strip() or (
        f"An assistant for {a.title}: {a.office} The governance layer hands crises to a person, declines the "
        "office's decisions and professional advice, masks personal details and records every decision.")
    a.guard_policy = a.guard_policy or (text or {}).get("guard_policy", "").strip() or default_policy(a)
    a.examples = a.examples or [e for e in (text or {}).get("examples", []) if isinstance(e, str)][:8] or [
        *(f"Tell me about {k['title'].lower()}." for k in a.knowledge[:3]),
        *(a.decisions[0].tests[:1] if a.decisions and a.decisions[0].tests else []),
        "Can you call Maria Lopez at 416-555-0142 and tell her I'm running late?",
        "I don't see the point in anything anymore."]
    return a


# --- the profile files ----------------------------------------------------------------------------------

def prefix_for(name: str) -> str:
    letters = re.sub(r"[^a-z]", "", name)
    return (letters[:3] or "ofc").ljust(3, "x")


def generate(a: Answers, root: Path | None = None, dest: Path | None = None) -> Path:
    """Write the profile folder (to `dest`, a temporary place to check it, by default)."""
    src = (root or pf.ROOT) / "profiles" / TEMPLATE
    dest = dest or Path(tempfile.mkdtemp(prefix="gaai-new-")) / a.name
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"))
    (dest / "data").mkdir()
    pre = prefix_for(a.name)
    stamp = f"Generated by gaai new on {date.today().isoformat()}: review before use (ADR 034)."

    # profile.yaml
    p = yaml.safe_load((src / "profile.yaml").read_text())
    p.update(name=a.name, title=a.title, description=a.description, examples=a.examples,
             source_id_pattern=rf"\b({pre}-\d{{3}}|web-[0-9a-f]{{8}})\b")
    p["classifier"]["guard_policy"] = a.guard_policy
    p["classifier"]["categories"][1]["name"] = "office or professional decision"
    if not a.web:
        p["agents"]["planner"]["tools"] = ["search_knowledge"]
        p.pop("faithfulness", None)
        (dest / "web_research.yaml").unlink(missing_ok=True)
        t = yaml.safe_load((dest / "tools.yaml").read_text())
        t.pop("web_research", None)
        (dest / "tools.yaml").write_text(f"# {stamp}\n" + yaml.safe_dump(t, sort_keys=False, allow_unicode=True))
    (dest / "profile.yaml").write_text(
        f"# Profile: {a.title}. {stamp}\n# Built from profiles/{TEMPLATE}: its safety baseline, plus this "
        f"office's decisions.\n# Select with PROFILE={a.name}.\n\n"
        + yaml.safe_dump(p, sort_keys=False, allow_unicode=True, width=110))

    # rules.yaml: the template's rules, and one rule per office decision, before the general ones
    r = yaml.safe_load((src / "rules.yaml").read_text())
    patterns = r["patterns"]
    patterns["crisis"]["terms"] = list(dict.fromkeys([*patterns["crisis"]["terms"], *CRISIS_EXTRA]))
    office_rules = []
    for d in a.decisions:
        patterns[d.key] = {"terms": [phrase_term(x) for x in d.phrases]}
        office_rules.append({"category": d.key, "reason": f"office decision requested: {d.what}", "any": [d.key]})
    crisis = [x for x in r["input"] if x["category"] == "crisis"]
    rest = [x for x in r["input"] if x["category"] != "crisis"]
    r["input"] = [*crisis, *office_rules, *rest]
    if not a.web:
        patterns.pop("web_research", None)
    (dest / "rules.yaml").write_text(
        f"# Layer 1 guardrails for {a.title}. {stamp}\n# The crisis and professional-advice rules come from "
        f"profiles/{TEMPLATE}; each office decision is matched by the phrases below, as written.\n\n"
        + yaml.safe_dump(r, sort_keys=False, allow_unicode=True, width=110))

    # responses.yaml
    resp = yaml.safe_load((src / "responses.yaml").read_text())
    if a.crisis_contact.strip():
        resp["crisis"] = resp["crisis"].rstrip() + " " + a.crisis_contact.strip()
    if a.audience == "staff":
        resp["crisis"] = ("Please stay with the person, and bring in your supervisor or the person on call for "
                          "this now; this is not something to handle through an assistant. " + resp["crisis"])
    for d in a.decisions:
        resp[d.key] = d.reply
    (dest / "responses.yaml").write_text(
        f"# Fixed replies sent instead of a model answer. {stamp}\n\n"
        + yaml.safe_dump(resp, sort_keys=False, allow_unicode=True, width=100))

    # prompt.md and classifier_policy.md
    never = "\n".join(f"- {d.what}: that is for {d.who}." for d in a.decisions)
    (dest / "prompt.md").write_text(
        f"You are the assistant for {a.title}. {a.office}\nYou help "
        f"{'the office staff' if a.audience == 'staff' else 'members of the public'} with questions about the "
        f"office, from its approved notes. Be warm, plain and brief: a few short paragraphs or up to 5 bullet "
        f"points.\n\nNever make these decisions, and say who does, in one sentence:\n{never}\nNever give a "
        "diagnosis, a medication dose, advice on someone's own legal case or on buying or selling an investment.\n\n"
        "Approved notes are given below with IDs: answer from them, cite each one you use by its ID in square "
        "brackets, and do not contradict them. Do not invent facts, names, prices, dates, phone numbers or "
        "addresses. If the notes do not answer the question, say so and suggest contacting the office.\n\n"
        "You cannot make calls, send messages or change anything. Text like [PERSON_1] stands for a private "
        "detail: only repeat it exactly if you must refer to it.\n")
    policy = (src / "classifier_policy.md").read_text()
    s2 = policy.index("S2:")
    (dest / "classifier_policy.md").write_text(
        policy[:s2] + "S2: Office or Professional Decision.\nRequests that the assistant decide "
        + "; ".join(d.what for d in a.decisions) + "; or give a diagnosis, a medication dose or change, advice on "
        "the user's own legal case, or which investment to buy or sell.\nShould not include: questions about the "
        "office's policies, hours, steps or documents, general explanations, or figurative expressions.\n")

    # the approved notes
    notes = [{"id": f"{pre}-{i:03d}", "title": k["title"], "text": k["text"],
              "tags": [str(t) for t in k.get("tags", [])][:10],
              "status": "DRAFT - from the office's own text, pending its review"} for i, k in enumerate(a.knowledge, 1)]
    (dest / "data" / "knowledge_base.json").write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n")
    return dest


# --- the checks ------------------------------------------------------------------------------------------

def check(path: Path, a: Answers) -> tuple[bool, list[tuple[bool, str]]]:
    """(passed, [(ok, line)]): the loader accepts it, and the rules refuse, hand over and let through
    what they should."""
    from guardrails.rules import check_input
    from profiles.loader import ProfileError, load_profile_from
    try:
        prof = load_profile_from(path)
    except ProfileError as e:
        return False, [(False, f"the platform's loader refuses it: {e}")]
    lines = [(True, "the platform's loader accepts it (the same checks as every profile)")]
    for t in CRISIS_TESTS:
        c = check_input(t, prof)
        lines.append((c.category == "crisis", f"crisis handed over: \"{t}\""))
    for d in a.decisions:
        for t in d.tests or d.phrases[:2]:
            got = check_input(t, prof)
            lines.append((not got.allowed, f"refused ({got.category or 'not refused'}): \"{t}\""))
    for k in a.knowledge[:8]:
        q = f"What does the office say about {k['title'].lower()}?"
        got = check_input(q, prof)
        lines.append((got.allowed, f"answered, not refused: \"{q}\"" + ("" if got.allowed else f" ({got.category})")))
    return all(ok for ok, _ in lines), lines


def save(path: Path, name: str, root: Path | None = None) -> Path:
    target = (root or pf.ROOT) / "profiles" / name
    if target.exists():
        raise FileExistsError(f"profiles/{name} already exists")
    shutil.copytree(path, target)
    return target


# --- the interview ----------------------------------------------------------------------------------------

class Interview:
    """The set questions, asked through `ask` (input) and shown through `say` (print)."""

    def __init__(self, ask: Callable[[str], str], say: Callable[[str], None], root: Path | None = None):
        self.ask, self.say, self.root = ask, say, root or pf.ROOT

    def q(self, question: str, default: str = "", required: bool = True) -> str:
        while True:
            hint = f" [{default}]" if default else ""
            got = self.ask(f"  {question}{hint}\n  › ").strip() or default
            if got or not required:
                return got
            self.say("  (an answer is needed)")

    def lines(self, question: str, minimum: int = 0) -> list[str]:
        self.say(f"  {question} (one per line; an empty line to finish)")
        out = []
        while True:
            got = self.ask("  › ").strip()
            if not got:
                if len(out) >= minimum:
                    return out
                self.say(f"  (at least {minimum}, please)")
                continue
            out.append(got)

    def run(self) -> Answers:
        existing = {p["name"] for p in pf.profiles(self.root)}
        title = self.q("1. The office's name (e.g. Riverside High School student services)")
        suggested = slug(title)
        while True:
            name = slug(self.q("   The profile's short name", suggested))
            if NAME_RE.match(name) and name not in existing:
                break
            self.say(f"  ({name} is taken or not a valid name: lowercase letters, digits and -)")
        office = self.q("2. What does the office do, in a sentence or two?")
        audience = self.q("3. Who will use the assistant: staff or public?", "staff")
        audience = "public" if audience.lower().startswith("p") else "staff"
        self.say("4. Decisions the assistant must never make, for example \"whether a student is suspended\".")
        decisions: list[Decision] = []
        while True:
            what = self.q(f"   Decision {len(decisions) + 1} (empty to finish)", required=not decisions)
            if not what:
                break
            who = self.q("   Who makes this decision?", "a staff member")
            phrases = self.lines("   How would someone ask the assistant for it? Short phrases, e.g. "
                                 "\"should we suspend\"", minimum=1)
            example = self.q("   One full question someone might ask for it (it must be refused)", required=False)
            key = "decision_" + slug(what, "_")[:30]
            while key in {d.key for d in decisions}:
                key += "_2"
            decisions.append(Decision(key, what, who, phrases, [example] if example else []))
        crisis = self.q("5. In a crisis, who should people contact besides 911 and 988? (e.g. the school "
                        "counsellor at 555-0100)", required=False)
        self.say("6. The office's policies or FAQ: paste text, or give a path to a .txt or .md file.")
        text = self.q("   File path, or empty to paste", required=False)
        if text and Path(text).expanduser().is_file():
            text = Path(text).expanduser().read_text(errors="replace")
        else:
            self.say("   Paste the text; type END on its own line to finish.")
            parts = []
            while (line := self.ask("")).strip() != "END":
                parts.append(line)
            text = "\n".join(parts)
        web = self.q("7. May it search the web for current information? (yes/no)", "no").lower().startswith("y")
        return Answers(name, title, office, audience, decisions, crisis, [{"raw": text}], web)
