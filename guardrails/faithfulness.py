"""Faithfulness check (ADR 022): a second model reads the answer against the sources it was given
and names claims the sources do not support. An answer with unsupported claims is withheld.

Why a second model: the model that wrote the answer cannot be its own judge. The profile names the
judge (a different family from the answering model; by default Meta's Llama 3.1 8B against Qwen),
and the audit records which judge ran and what it found, never the text.

When it runs: only when the profile has a `faithfulness` block, and only for answers written from
tool results that the block lists (for example web research). Records and approved notes already
have the grounding check; web pages need more, because a model summarising three pages drifts.

The judge is asked for JSON only. Anything it says that is not JSON is treated as "could not judge",
and the profile decides whether that withholds the answer (strict) or lets it through with a note.
"""

import json
import os
import re
from dataclasses import dataclass

from guardrails.rules import GuardrailDecision

JUDGE_PROMPT = """You are checking an assistant's ANSWER against the SOURCES it was given (web pages).
Your job is to catch invented or contradicted facts, not to grade the wording.

A claim is UNSUPPORTED only if it states a specific fact that the SOURCES do not state, or that
the SOURCES contradict. Specific facts are: a person's name, who holds an office, a date or
year, a number, a price, a rule or law, a direct quotation, a place. Names and dates are the
facts most often invented from memory: check each one against the SOURCES' own words, and if
the SOURCES say a different person or date, the claim is unsupported.

The following are SUPPORTED and must not be listed: summaries or paraphrases of what the
SOURCES say; general descriptions ("a manga series about pirates") when the SOURCES describe the
same thing; the assistant's advice to see a professional or to read the pages; statements that
the information was looked up or when it was fetched.

Reply with JSON only, no other text:
{"unsupported": [{"claim": "<the specific fact>", "why": "not in sources" | "contradicted: <what the sources say>"}],
 "verdict": "supported" | "unsupported"}
If every specific fact is in the SOURCES, reply {"unsupported": [], "verdict": "supported"}."""


@dataclass(frozen=True)
class FaithfulnessConfig:
    judge: dict                       # a model entry (profile.yaml models), never the answering model
    for_tools: tuple[str, ...]        # run only when one of these tools returned the notes
    strict: bool = True               # an unreadable judge reply withholds the answer
    mode: str = "withhold"            # withhold: any unsupported fact withholds the answer ·
                                      # warn: a fact the pages CONTRADICT withholds; a fact they do not
                                      # mention lets the answer through with a warning (GuardrailDecision
                                      # allowed, info["warning"] set). The office chooses.


def load_config(profile) -> FaithfulnessConfig | None:
    raw = getattr(profile, "faithfulness", None)
    if not raw:
        return None
    judge = profile.models.get(raw["judge"])
    if judge is None:
        raise ValueError(f"faithfulness: judge '{raw['judge']}' is not in models")
    return FaithfulnessConfig(judge, tuple(raw.get("for_tools") or ()), bool(raw.get("strict", True)),
                              str(raw.get("mode", "withhold")))


def parse_verdict(text: str) -> dict | None:
    """The judge's JSON, or None if it did not answer in JSON."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or "verdict" not in d:
        return None
    claims = d.get("unsupported")
    unsupported, contradicted = [], 0
    for c in claims if isinstance(claims, list) else []:
        if isinstance(c, dict):                           # {"claim": ..., "why": ...}
            text = str(c.get("claim", "")).strip()
            if text:
                why = str(c.get("why") or "")
                unsupported.append((text + (f" ({why})" if why else ""))[:200])
                contradicted += why.lower().startswith("contradicted")
        elif str(c).strip():
            unsupported.append(str(c)[:200])
    return {"verdict": str(d["verdict"]).lower().strip(), "unsupported": unsupported, "contradicted": contradicted}


def judge_messages(notes_text: str, answer: str) -> list[dict]:
    return [{"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"SOURCES:\n{notes_text}\n\nANSWER:\n{answer}"}]


def check(answer: str, notes_text: str, cfg: FaithfulnessConfig, chat, tools_used: list[str],
          profile=None) -> tuple[GuardrailDecision, dict]:
    """(decision, what the judge said: for the trace and audit, no text of the answer).
    `chat(model, messages, purpose)` is llm.providers.chat or a fake."""
    if cfg.for_tools and not any(t in cfg.for_tools for t in tools_used):
        return GuardrailDecision(True), {"ran": False}
    try:
        raw = chat(cfg.judge, judge_messages(notes_text, answer), purpose="judge")
    except Exception as e:              # noqa: BLE001  the judge is a model call; it can fail
        if cfg.strict:
            return _withhold(profile, "judge unavailable"), {"ran": True, "judge": cfg.judge["name"], "error": type(e).__name__}
        return GuardrailDecision(True), {"ran": True, "judge": cfg.judge["name"], "error": type(e).__name__}
    v = parse_verdict(raw)
    info = {"ran": True, "judge": cfg.judge["name"]}
    if v is None:
        info["unreadable"] = True
        return (_withhold(profile, "judge reply unreadable") if cfg.strict else GuardrailDecision(True)), info
    info["verdict"], info["unsupported"], info["contradicted"] = v["verdict"], len(v["unsupported"]), v["contradicted"]
    if not v["unsupported"]:                               # the list decides; a bare "unsupported" verdict
        return GuardrailDecision(True), info               # with nothing named is not a finding
    if os.environ.get("FAITHFULNESS_DEBUG"):      # development only: the claims go to the console, never
        print("faithfulness: unsupported claims:", json.dumps(v["unsupported"]), flush=True)   # never to the trace or audit
    if cfg.mode == "warn" and not v["contradicted"]:
        info["warning"] = True                             # delivered, with the not_confirmed note added
        return GuardrailDecision(True), info
    return _withhold(profile, f"{len(v['unsupported'])} unsupported claim(s)"
                     + (f", {v['contradicted']} contradicted by the pages" if v["contradicted"] else "")), info


def _withhold(profile, reason: str) -> GuardrailDecision:
    reply = profile.response("unfaithful") if profile is not None and "unfaithful" in profile.responses else (
        "I found sources but could not give an answer that sticks to them, so I am not giving one. "
        "The sources are listed; please read them directly.")
    return GuardrailDecision(False, "unfaithful", reason, reply)
