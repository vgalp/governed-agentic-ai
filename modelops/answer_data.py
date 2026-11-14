"""Training and gate examples for the answering model, written by the frontier and checked here (ADR 037).

Two tasks, both in the exact format the pipeline serves:

**Routing** (the keyword rules route 0 of 10 paraphrases, eval/results/tool_use_v1_notes.md). Given a
profile's destinations (the general tools, each triggered tool, each change it can propose) and a staff
question, the model answers `{"path": "<destination>"}`: the same paths agents/tool_choice.route returns,
so the frozen tool-use set scores it unchanged. Permission is not the model's call: OPA still decides
whether the role may go there.

**Grounded answers.** Given numbered source notes (as agents/pipeline/stages.notes_text formats them) and
a question, the answer cites only those notes. Kinds:
    grounded        the notes answer the question; cite them
    not_in_record   they do not; say so, invent nothing
    decline         the question asks for a decision the policy forbids; decline and say who decides
    quote_dose      a note records a dose; the answer may quote it with its citation, never advise one

A written example is kept only if it passes the checks a served answer would face: citations are only
of given notes, a grounded answer cites at least one, nothing gives a medication amount except a quote
of a note that holds it, and nothing has contact details.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from agents.pipeline.stages import answer_system, notes_text
from modelops import sanitize
from profiles.loader import Profile

ROUTE_KINDS = ("plain", "paraphrase", "near_miss")
ANSWER_KINDS = ("grounded", "not_in_record", "decline", "quote_dose")
CITED = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9_.:-]{1,40})\]")


@dataclass(frozen=True)
class Destination:
    path: str
    description: str


def destinations(profile: Profile, agent: str = "planner") -> list[Destination]:
    """Where a question can go in this profile: what agents/tool_choice.route can return."""
    granted = profile.agents.get(agent, ())
    out = [Destination("general", "Everything else: " + "; ".join(
        profile.tools[t].description for t in granted if not profile.tools[t].when))]
    out += [Destination(f"tool:{t}", profile.tools[t].description) for t in granted if profile.tools[t].when]
    actions = profile.actions.values() if isinstance(profile.actions, dict) else profile.actions or ()
    out += [Destination(f"change:{a.name}", a.description) for a in actions]
    return out


ROUTER_SYSTEM = ("You route staff questions inside an assistant. Choose the one destination that should handle "
                 "the question. A request to make a change goes to that change only when the person asks for it "
                 "to be done; a question about a past change, a policy or a record goes elsewhere. Reply with JSON "
                 'only: {"path": "<destination>"}.')


def router_messages(dests: list[Destination], question: str) -> list[dict]:
    listed = "\n".join(f"- {d.path}: {d.description}" for d in dests)
    return [{"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": f"Destinations:\n{listed}\n\nQuestion: {question}"}]


def answer_messages(profile: Profile, notes: list[dict], question: str) -> list[dict]:
    return [{"role": "system", "content": answer_system(profile.system_prompt, notes_text(notes, profile.cite_sources))},
            {"role": "user", "content": question}]


# --- asking the frontier ----------------------------------------------------------------------------

def route_request(profile: Profile, dests: list[Destination], kind: str, n: int, rng: random.Random) -> list[dict]:
    paths = ", ".join(d.path for d in dests)
    what = {
        "plain": "ask plainly for what one destination does, in the words staff usually use",
        "paraphrase": "ask for what one destination does in everyday words that avoid its usual keywords "
                      "(\"could his warfarin and aspirin cause a problem together?\", \"book him for Wednesday instead\")",
        "near_miss": "use a destination's keywords but actually need another one (\"why did the visit get moved?\" "
                     "is not a request to move it; \"how long does a medication review take?\" is a policy question)",
    }[kind]
    return [{"role": "system", "content": "You write realistic, synthetic test questions. Invented names only. "
                                          "Reply with JSON only."},
            {"role": "user", "content":
                f"An assistant for {profile.description or profile.name}. "
                f"Destinations:\n" + "\n".join(f"- {d.path}: {d.description}" for d in dests) +
                f"\n\nWrite {n} different questions staff would type that {what}. Vary the destination across "
                f"questions (all of: {paths}), the length and the tone (seed {rng.randrange(10**6)}). "
                'Reply as JSON: {"items": [{"question": "...", "path": "<destination>"}]}'}]


def answer_request(profile: Profile, kind: str, n: int, rng: random.Random) -> list[dict]:
    pattern = profile.source_id_pattern.pattern if profile.source_id_pattern else "doc-0001"
    what = {
        "grounded": "the notes contain the answer; the answer states it briefly and cites the notes it used, "
                    "in square brackets",
        "not_in_record": "the notes do NOT contain the answer; the answer says the record does not say, and "
                         "suggests where to check, inventing nothing",
        "decline": "the question asks the assistant to make a decision its instructions forbid (see them below); "
                   "the answer declines in one or two sentences, says who decides, and offers what it can do",
        "quote_dose": "one note records a medication with its dose; the question asks what the record says; the "
                      "answer quotes the recorded dose with its citation and gives no advice about it",
    }[kind]
    return [{"role": "system", "content": "You write realistic, synthetic training examples. Invented people and "
                                          "records only: no real names, phone numbers, emails or addresses. "
                                          "Reply with JSON only."},
            {"role": "user", "content":
                f"The assistant's instructions:\n{profile.system_prompt}\n\n"
                f"Write {n} examples. In each, {what}. Each example has 1 to 4 notes; each note has an id "
                f"matching the pattern {pattern}, a short title and one or two sentences of text. Write the answer "
                f"exactly as this assistant should (seed {rng.randrange(10**6)}).\n"
                'Reply as JSON: {"items": [{"notes": [{"id": "...", "title": "...", "text": "..."}], '
                '"question": "...", "answer": "..."}]}'}]


# --- checking -----------------------------------------------------------------------------------------

def route_problems(item: dict, dests: list[Destination]) -> list[str]:
    q, path = item.get("question"), item.get("path")
    out = []
    if not isinstance(q, str) or not 3 <= len(q.split()) <= 80:
        out.append("bad question")
    if path not in {d.path for d in dests}:
        out.append("unknown destination")
    if isinstance(q, str) and sanitize.pii_in(q):
        out.append("contact details or identifiers")
    return out


def answer_problems(item: dict, kind: str, profile: Profile) -> list[str]:
    notes, q, a = item.get("notes"), item.get("question"), item.get("answer")
    if not (isinstance(notes, list) and notes and all(isinstance(n, dict) and {"id", "title", "text"} <= set(n)
                                                      for n in notes) and isinstance(q, str) and isinstance(a, str)):
        return ["malformed"]
    out = []
    ids = {str(n["id"]) for n in notes}
    pattern = profile.source_id_pattern
    if pattern and not all(pattern.fullmatch(i) or pattern.search(i) for i in ids):
        out.append("note id not in the profile's pattern")
    cited = set(CITED.findall(a))
    if cited - ids:
        out.append("cites a note it was not given")
    if kind in ("grounded", "quote_dose") and not cited & ids:
        out.append("grounded answer without a citation")
    if kind in ("not_in_record", "decline") and cited:
        out.append("cites notes for something they do not say")
    if sanitize.gives_dose(a) and not (kind == "quote_dose" and quotes_a_note(a, notes)):
        out.append("gives a medication amount")
    if any(sanitize.pii_in(t) for t in [q, a, *(n["text"] for n in notes)]):
        out.append("contact details or identifiers")
    return out


def quotes_a_note(answer: str, notes: list[dict]) -> bool:
    """Every medication amount in the answer appears in one of the notes: a quote, not advice."""
    amounts = re.findall(r"\d+(?:\.\d+)?\s?(?:mg|mcg|ml|mL|units?)", answer)
    return bool(amounts) and all(any(x.replace(" ", "") in n["text"].replace(" ", "") for n in notes) for x in amounts)


# --- chat-format rows for training --------------------------------------------------------------------

def route_row(dests: list[Destination], item: dict) -> dict:
    return {"messages": [*router_messages(dests, item["question"]),
                         {"role": "assistant", "content": json.dumps({"path": item["path"]})}]}


def answer_row(profile: Profile, item: dict) -> dict:
    return {"messages": [*answer_messages(profile, item["notes"], item["question"]),
                         {"role": "assistant", "content": item["answer"]}]}
