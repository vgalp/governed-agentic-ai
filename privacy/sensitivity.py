"""Recognise sensitive categories of health information in a record or a message (ADR 038).

Some health information is protected more strictly than the rest: substance use treatment, mental
health, HIV, sexual and reproductive health, abuse, genetics. This module says which of those
categories a FHIR resource or a piece of text falls in. It decides nothing: who may see a category,
and whether it may leave the building, are policy (the roles and routing policies), which ask it.

    from privacy import sensitivity
    detector = sensitivity.Detector(sensitivity.load())
    detector.of_resource(condition)     # [Finding("substance_use", "code", "icd10:F11.20")]
    detector.of_text("on methadone")    # [Finding("substance_use", "medication", "methadone")]

    uv run python -m privacy.sensitivity "Is Maria Santos still on methadone?"

Four kinds of evidence, strongest first:

    label        the source system marked the record (meta.security). Trusted as it is.
    code         an ICD-10-CM code in the category's list.
    medication   a medication that is mostly used for the category.
    text         a phrase in the category's list.
    model        a text classifier, when one is given (`Detector(model=...)`): for wording the lists
                 cannot know, such as "finishes a fifth of vodka most nights".

The lists are in privacy/sensitivity.yaml. They are a starting set for development, not a clinical
value set: read the note at the top of that file.

A finding's `evidence` names the code or list entry that matched, never the record's own text, so it
can be logged. `summary()` gives the audit form: categories and kinds of evidence only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT = Path(__file__).resolve().parent / "sensitivity.yaml"

ACT_CODE = ("http://terminology.hl7.org/CodeSystem/v3-ActCode", "http://hl7.org/fhir/v3/ActCode")
CONFIDENTIALITY = ("http://terminology.hl7.org/CodeSystem/v3-Confidentiality", "http://hl7.org/fhir/v3/Confidentiality")
ICD10 = ("http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10",
         "urn:oid:2.16.840.1.113883.6.90", "urn:oid:2.16.840.1.113883.6.3")
LABEL_KEYS = ("security", "securityLabel")       # where FHIR carries security labels
TEXT_KEYS = ("display", "text", "div", "valueString", "description", "conclusion", "comment", "title",
             "patientInstruction")
SOURCES = ("label", "code", "medication", "text", "model")
CATEGORY_KEYS = {"title", "hl7_labels", "confidentiality", "icd10", "drugs", "phrases", "phrases_exact_case"}
MAX_TEXT = 20_000                                # characters of one text read; records are far shorter

TextModel = Callable[[str], Mapping[str, float]]  # text -> probability per category


class ValueSetError(ValueError):
    """privacy/sensitivity.yaml (or a replacement) is not usable. Nothing is detected with a broken file."""


@dataclass(frozen=True)
class Finding:
    category: str
    source: str        # one of SOURCES
    evidence: str      # the code or list entry that matched; never text from the record


@dataclass(frozen=True)
class Category:
    key: str
    title: str
    icd10: tuple[str, ...]                 # normalized prefixes: upper case, no dot
    drugs: re.Pattern | None
    phrases: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class ValueSets:
    categories: Mapping[str, Category]
    labels: Mapping[str, tuple[str, ...]]            # ActCode label -> categories
    confidentiality: Mapping[str, tuple[str, ...]]   # Confidentiality code -> categories
    sha256: str                                      # of the file: recorded with every evaluation


def _strings(where: str, value, lower: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueSetError(f"{where} must be a list of non-empty text values")
    return tuple(v.strip().lower() if lower else v.strip() for v in value)


def _pattern(where: str, expression: str, flags: int) -> re.Pattern:
    try:
        return re.compile(rf"(?<!\w)(?:{expression})(?!\w)", flags)
    except re.error as e:
        raise ValueSetError(f"{where}: {expression!r} is not a valid regular expression ({e})") from None


def icd10_key(code: str) -> str:
    """F11.20 -> F1120: how codes and prefixes are compared."""
    return code.replace(".", "").strip().upper()


def parse(raw, sha256: str = "") -> ValueSets:
    """Value sets from the parsed YAML, checked: an unknown key or a bad pattern stops loading."""
    if not isinstance(raw, dict) or not isinstance(raw.get("categories"), dict) or not raw["categories"]:
        raise ValueSetError("the file needs a 'categories' mapping with at least one category")
    categories: dict[str, Category] = {}
    labels: dict[str, list[str]] = {}
    confidentiality: dict[str, list[str]] = {}
    for key, spec in raw["categories"].items():
        where = f"categories.{key}"
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueSetError(f"{where}: a category name is lower-case letters, digits and underscores")
        if not isinstance(spec, dict) or not isinstance(spec.get("title"), str) or not spec["title"].strip():
            raise ValueSetError(f"{where} needs a title")
        unknown = set(spec) - CATEGORY_KEYS
        if unknown:
            raise ValueSetError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
        for code in _strings(f"{where}.hl7_labels", spec.get("hl7_labels")):
            labels.setdefault(code, []).append(key)
        for code in _strings(f"{where}.confidentiality", spec.get("confidentiality")):
            confidentiality.setdefault(code, []).append(key)
        drugs = _strings(f"{where}.drugs", spec.get("drugs"), lower=True)
        phrases = [_pattern(f"{where}.phrases", p, re.IGNORECASE) for p in _strings(f"{where}.phrases", spec.get("phrases"))]
        phrases += [_pattern(f"{where}.phrases_exact_case", p, 0)
                    for p in _strings(f"{where}.phrases_exact_case", spec.get("phrases_exact_case"))]
        categories[key] = Category(
            key=key, title=spec["title"].strip(),
            icd10=tuple(icd10_key(c) for c in _strings(f"{where}.icd10", spec.get("icd10"))),
            drugs=_pattern(f"{where}.drugs", "|".join(re.escape(d) for d in drugs), re.IGNORECASE) if drugs else None,
            phrases=tuple(phrases))
    return ValueSets(categories, {k: tuple(v) for k, v in labels.items()},
                     {k: tuple(v) for k, v in confidentiality.items()}, sha256)


@lru_cache(maxsize=8)
def _load(path: str, modified: float) -> ValueSets:
    data = Path(path).read_bytes()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as e:
        raise ValueSetError(f"{path} is not valid YAML: {e}") from None
    return parse(raw, hashlib.sha256(data).hexdigest())


def load(path: Path = DEFAULT) -> ValueSets:
    """The value sets in a file, read again only when the file changes."""
    try:
        return _load(str(path), path.stat().st_mtime)
    except OSError as e:
        raise ValueSetError(f"cannot read {path}: {e.strerror or e}") from None


class Detector:
    """Finds the sensitive categories of a text or a FHIR resource. Holds no state between calls."""

    def __init__(self, sets: ValueSets, model: TextModel | None = None, threshold: float = 0.5):
        if not 0.0 < threshold < 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.sets, self.model, self.threshold = sets, model, threshold

    def of_text(self, text: str) -> list[Finding]:
        """Categories named or implied by this text: the medication and phrase lists, then the model."""
        text = (text or "")[:MAX_TEXT]
        if not text.strip():
            return []
        found: list[Finding] = []
        for c in self.sets.categories.values():
            if c.drugs and (m := c.drugs.search(text)):
                found.append(Finding(c.key, "medication", m.group(0).lower()))
            for p in c.phrases:
                if p.search(text):
                    found.append(Finding(c.key, "text", "phrase list"))
                    break
        if self.model is not None:
            for category, probability in self.model(text).items():
                if category not in self.sets.categories:
                    raise ValueError(f"the text model returned an unknown category: {category!r}")
                if probability >= self.threshold:
                    found.append(Finding(category, "model", f"{probability:.2f}"))
        return _unique(found)

    def of_resource(self, resource: Mapping) -> list[Finding]:
        """Categories of one FHIR resource: its security labels, its codes, then its text."""
        if not isinstance(resource, Mapping):
            raise TypeError("a FHIR resource is a mapping")
        found: list[Finding] = []
        texts: list[str] = []
        for under_label, node in _nodes(resource, False):
            system, code = node.get("system"), node.get("code")
            if isinstance(system, str) and isinstance(code, str):
                found += self._coded(system, code, under_label)
            for key in TEXT_KEYS:
                value = node.get(key)
                if isinstance(value, str):
                    texts.append(_plain(value) if key == "div" else value)
        return _unique(found + self.of_text("\n".join(texts)))

    def _coded(self, system: str, code: str, under_label: bool) -> list[Finding]:
        if under_label and system in ACT_CODE:
            return [Finding(c, "label", f"hl7:{code}") for c in self.sets.labels.get(code, ())]
        if under_label and system in CONFIDENTIALITY:
            return [Finding(c, "label", f"confidentiality:{code}") for c in self.sets.confidentiality.get(code, ())]
        if system in ICD10:
            key = icd10_key(code)
            return [Finding(c.key, "code", f"icd10:{code.strip().upper()}")
                    for c in self.sets.categories.values() if any(key.startswith(p) for p in c.icd10)]
        return []


def _nodes(node, under_label: bool) -> Iterator[tuple[bool, Mapping]]:
    """Every mapping in a resource, with whether it sits under a security-label element."""
    if isinstance(node, Mapping):
        yield under_label, node
        for key, value in node.items():
            yield from _nodes(value, under_label or key in LABEL_KEYS)
    elif isinstance(node, list):
        for item in node:
            yield from _nodes(item, under_label)


def _plain(html: str) -> str:
    """The text of a FHIR narrative (XHTML), without its tags."""
    return re.sub(r"<[^>]+>", " ", html)


def _unique(found: Iterable[Finding]) -> list[Finding]:
    return list(dict.fromkeys(found))


def categories(found: Iterable[Finding]) -> tuple[str, ...]:
    """The categories found, sorted, each once."""
    return tuple(sorted({f.category for f in found}))


def summary(found: Iterable[Finding]) -> dict[str, list[str]]:
    """For the audit: category -> kinds of evidence. No codes, no matched words, no record text."""
    out: dict[str, set[str]] = {}
    for f in found:
        out.setdefault(f.category, set()).add(f.source)
    return {c: sorted(s, key=SOURCES.index) for c, s in sorted(out.items())}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Show the sensitive categories of a piece of text.")
    ap.add_argument("text")
    ap.add_argument("--sets", type=Path, default=DEFAULT, help="value sets file (default: privacy/sensitivity.yaml)")
    args = ap.parse_args()
    sets = load(args.sets)
    found = Detector(sets).of_text(args.text)
    if not found:
        print("No sensitive category found by the lists.")
    for category, sources in summary(found).items():
        print(f"{category} ({sets.categories[category].title}): {', '.join(sources)}")


if __name__ == "__main__":
    main()
