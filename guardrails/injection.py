"""Prompt injection in tool results: text that comes back from a tool and tries to instruct the AI.

A record note, a policy document or an email body is DATA. If it contains "ignore your previous
instructions", fake role markers, hidden characters or a link built to send data away, a model that
reads it may follow it. The input guardrails never see this text, because it does not come from the
person asking: it comes from the tools. So every tool result is checked here, in the gateway, before
any model or outside assistant receives it (ADR 016).

What happens to a field that looks like instructions: it is withheld, replaced by a short notice that
says why, and the call's audit event records how many fields were withheld. Invisible characters that
can hide instructions (Unicode tag characters, runs of zero-width characters) are always removed.

Text is normalised before it is matched, so the patterns cannot be dodged by how the letters are
written: compatibility forms are folded (NFKC: a fullwidth "Ｉgnore" is "Ignore"), every invisible
format character is dropped (zero-width spaces and joiners, soft hyphens, direction marks, tag
characters), common look-alike letters from other alphabets are mapped to Latin ones, and runs of
spaces are collapsed. The normalised copy is only for matching; what is passed on is the original
text, with invisible characters that sit inside a word removed (that is how instructions are hidden).

Limits, stated plainly: these are fixed patterns for the common, known forms. A determined attacker
can word an instruction that matches none of them. This check is one layer, a signal rather than a
gate: the model is also told that tool results are data, the tools are read-only, answers from web
pages are judged by a second model, and every result is audited.
"""

import re
import unicodedata

WITHHELD = "[withheld: this text from {tool} looked like instructions to the AI, not data]"

_FLAGS = re.IGNORECASE | re.MULTILINE
PATTERNS = {
    "override": re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,30}\b(previous|prior|above|earlier|preceding|"
        r"all|any|your|the)\b[^.\n]{0,20}\b(instructions?|prompts?|rules|guidelines|guardrails|messages|context)\b",
        _FLAGS),
    "new_instructions": re.compile(r"\b(new|updated|revised|real|actual)\s+(system\s+)?instructions?\s*:", _FLAGS),
    "role_change": re.compile(r"\b(you are now|from (now|this point|here)( on)?,?\s+you\s+(are|will|must|should|act)|"
                              r"you (must|will|should) now act as)\b", _FLAGS),
    "forget": re.compile(r"\b(forget|ignore|disregard)\s+(everything|all of (that|this)|what you were told)"
                         r"(\s+(above|before|earlier|so far))?\b", _FLAGS),
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
# An invisible format character between two Latin letters: the way a word is split so a pattern
# misses it. Only Latin: in Persian, Arabic or Indic text a zero-width joiner or non-joiner is spelling.
_HIDDEN_IN_WORD = re.compile(r"(?<=[A-Za-z])[\u00ad\u200b-\u200f\u2060-\u2064\ufeff\u180e]+(?=[A-Za-z])")
# Look-alike letters from Cyrillic and Greek that pass for Latin in most fonts.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ɡ": "g", "һ": "h", "ӏ": "l", "ո": "n", "ս": "u", "ԝ": "w",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "ε": "e", "ι": "i", "ο": "o", "ρ": "p", "υ": "u", "ν": "v",
    "Α": "A", "Β": "B", "Ε": "E", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X",
})


def normalise(text: str) -> str:
    """The text as the patterns should see it: compatibility forms folded, invisible format
    characters dropped, look-alike letters mapped to Latin, whitespace collapsed."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    t = t.translate(_CONFUSABLES)
    return re.sub(r"\s+", " ", t)


def clean_invisible(text: str) -> tuple[str, bool]:
    """Text without characters that can hide instructions; True if any were removed."""
    out = _HIDDEN_IN_WORD.sub("", _ZERO_WIDTH_RUN.sub("", _TAG_CHARS.sub("", text)))
    return out, out != text


def findings(text: str) -> list[str]:
    """Names of the patterns this text matches (empty: looks like data). Matched on the normalised
    text, so spelling tricks do not hide an instruction."""
    t = normalise(text)
    return [name for name, rx in PATTERNS.items() if rx.search(t)]


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
