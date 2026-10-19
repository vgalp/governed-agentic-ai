"""Prompt injection in tool results: text that comes back from a tool and tries to instruct the AI.

A record note, a policy document or an email body is DATA. If it contains "ignore your previous
instructions", fake role markers, hidden characters or a link built to send data away, a model that
reads it may follow it. The input guardrails never see this text, because it does not come from the
person asking: it comes from the tools. So every tool result is checked here, in the gateway, before
any model or outside assistant receives it (ADR 016).

What happens to a field that looks like instructions: it is withheld, replaced by a short notice that
says why, and the call's audit event records how many fields were withheld. Invisible characters that
can hide instructions (Unicode tag characters, runs of zero-width characters) are always removed.

Limits, stated plainly: these are fixed patterns for the common, known forms. A determined attacker
can word an instruction that matches none of them. This check is one layer: the model is also told
that tool results are data, the tools are read-only, and every result is audited.
"""

import re

WITHHELD = "[withheld: this text from {tool} looked like instructions to the AI, not data]"

_FLAGS = re.IGNORECASE | re.MULTILINE
PATTERNS = {
    "override": re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,30}\b(previous|prior|above|earlier|preceding|"
        r"all|any|your|the)\b[^.\n]{0,20}\b(instructions?|prompts?|rules|guidelines|guardrails|messages|context)\b",
        _FLAGS),
    "new_instructions": re.compile(r"\b(new|updated|revised|real|actual)\s+(system\s+)?instructions?\s*:", _FLAGS),
    "role_change": re.compile(r"\b(you are now|from now on,?\s+you\s+(are|will|must|should))\b", _FLAGS),
    "role_marker": re.compile(r"(^\s*(system|assistant|developer)\s*:|<\|im_start\|>|<\|im_end\|>|\[/?INST\]|"
                              r"</?\s*system\s*>|^#{2,}\s*(system|instruction)s?\b)", _FLAGS),
    "prompt_leak": re.compile(r"\b(reveal|print|show|repeat|output)\b[^.\n]{0,20}\b(system prompt|your "
                              r"(instructions|prompt|rules)|hidden (instructions|prompt))\b", _FLAGS),
    "conceal": re.compile(r"\b(do not|don't|never)\s+(tell|inform|mention (this )?to|let)\s+the\s+(user|person|"
                          r"patient|staff)\b|\bwithout (telling|informing) the (user|person)\b", _FLAGS),
    "exfiltrate": re.compile(r"\b(send|email|post|upload|forward|transmit)\b[^.\n]{0,60}\b(to|at)\s+"
                             r"(https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+)", _FLAGS),
    "exfil_image": re.compile(r"!\[[^\]]*\]\(\s*https?://[^)\s]*\?[^)\s]*\)", _FLAGS),
}
_TAG_CHARS = re.compile("[\U000E0000-\U000E007F]")              # invisible "tag" letters
_ZERO_WIDTH_RUN = re.compile("[​-‏⁠-⁤﻿]{3,}")


def clean_invisible(text: str) -> tuple[str, bool]:
    """Text without characters that can hide instructions; True if any were removed."""
    out = _ZERO_WIDTH_RUN.sub("", _TAG_CHARS.sub("", text))
    return out, out != text


def findings(text: str) -> list[str]:
    """Names of the patterns this text matches (empty: looks like data)."""
    return [name for name, rx in PATTERNS.items() if rx.search(text)]


def sanitize(value, tool: str, found: list[str]):
    """The same structure with suspicious text fields withheld. Appends what it found to `found`."""
    if isinstance(value, str):
        text, hidden = clean_invisible(value)
        if hidden:
            found.append("invisible_characters")
        hits = findings(text)
        if hits:
            found.extend(hits)
            return WITHHELD.format(tool=tool)
        return text
    if isinstance(value, dict):
        return {k: sanitize(v, tool, found) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, tool, found) for v in value]
    return value


def check_result(result: dict, tool: str) -> tuple[dict, list[str]]:
    """(result safe to pass on, the kinds of injection found). Only the 'results' rows are data from
    the tool's sources; other keys (counts, notes from the tool itself) are checked the same way."""
    found: list[str] = []
    return sanitize(result, tool, found), found
