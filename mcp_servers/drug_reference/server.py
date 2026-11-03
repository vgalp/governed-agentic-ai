"""MCP server: drug reference (ADR 024). What the FDA label says about a medication, quoted.

For a clinician or nurse who wants to look a drug up, whether or not it is on a patient's list:
"what does the label say about apixaban in renal impairment", "warfarin and ibuprofen",
"contraindications of amiodarone". The answer is the label's own text for the sections asked
for, with the label's version date, its DailyMed link and an ID to cite. No model writes or
rewords it (tools.yaml answer: direct).

Safe by construction (drug_reference.yaml):
  - drugs, never patients: the tool gets the masked question; a placeholder for a person in it
    means the question was about a patient, and it is refused with a pointer to the medication
    review (the patient-specific tool, which reads the record under role control);
  - only the configured sections are quoted, and dosage and administration is not among them by
    default: a dose is the prescriber's decision (rules.yaml keeps catching dose questions);
  - decision words cancel the classifier exemption, so "can I give X with Y" is a decision, not
    a lookup;
  - the label is fetched on demand from openFDA, by ingredient resolved through RxNorm (both
    public U.S. government services; only the drug name is sent), kept in the store with the
    date fetched, and quoted with its version date. A drug with no label found says so.

Two drugs in one question: each label's mention of the other (the medication review's own
matching), or "neither label mentions the other in the sections on file".
"""

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from mcp_servers.medication_review import labels as fda
from profiles.loader import Profile, load_profile

MASK_TOKEN = re.compile(r"\[[A-Z_]+_\d+\]")
DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"


@dataclass(frozen=True)
class Config:
    store: Path
    fetch: str  # on_demand or never
    sections: dict[str, dict]  # key -> {title, ask: [words]}
    default_sections: tuple[str, ...]
    max_chars: int
    max_drugs: int
    not_a_drug: frozenset[str]
    class_terms: dict | None = None  # medication_review.yaml's class words, for the two-drug check


def load_config(profile: Profile) -> Config | None:
    path = profile.path / "drug_reference.yaml"
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sections = {
        k: {"title": str(v.get("title", k)), "ask": [str(a).lower() for a in v.get("ask") or []]}
        for k, v in (raw.get("sections") or {}).items()
    }
    if not sections:
        raise ValueError(f"{path.name}: sections is empty")
    if "dosage_and_administration" in sections:
        print(
            "drug reference: dosage_and_administration is quotable in this profile (an office choice; "
            "dose questions are still caught by rules.yaml)"
        )
    default = tuple(raw.get("default_sections") or list(sections)[:1])
    unknown = [d for d in default if d not in sections]
    if unknown:
        raise ValueError(f"{path.name}: default_sections names {unknown}, not in sections")
    fetch = raw.get("fetch", "on_demand")
    if fetch not in ("on_demand", "never"):
        raise ValueError(f"{path.name}: fetch must be on_demand or never")
    class_terms = {}
    mr_path = profile.path / "medication_review.yaml"
    if mr_path.exists():  # the review's class words ("anticoagulants" for warfarin)
        mr_raw = yaml.safe_load(mr_path.read_text(encoding="utf-8")) or {}
        class_terms = {k.lower(): v for k, v in (mr_raw.get("class_terms") or {}).items()}
    return Config(
        (profile.path / raw.get("store", "data/drug_reference.json")).resolve(),
        fetch,
        sections,
        default,
        int(raw.get("max_chars_per_section", 1500)),
        int(raw.get("max_drugs_per_question", 2)),
        frozenset(str(w).lower() for w in raw.get("not_a_drug") or []),
        class_terms,
    )


def load_store(cfg: Config) -> dict:
    if cfg.store.exists():
        return json.loads(cfg.store.read_text(encoding="utf-8"))
    return {"ingredients": {}, "lookups": {}}


def save_store(cfg: Config, store: dict) -> None:
    cfg.store.parent.mkdir(parents=True, exist_ok=True)
    cfg.store.write_text(json.dumps(store, indent=1, sort_keys=True), encoding="utf-8")


# --- which drugs and which sections the question asks about ---------------------------------------


def sections_asked(query: str, cfg: Config) -> list[str]:
    q = query.lower()
    asked = [k for k, v in cfg.sections.items() if any(w in q for w in v["ask"])]
    return asked or list(cfg.default_sections)


def known_drugs(query: str, store: dict) -> dict[str, str]:
    """{ingredient: how it was named} for ingredients and brand names already in the store."""
    q = query.lower()
    found = {}
    for ing, lab in (store.get("ingredients") or {}).items():
        names = [ing] + [b.lower() for b in lab.get("brand_names") or []]
        hit = next((n for n in names if re.search(r"\b" + re.escape(n) + r"\b", q)), None)
        if hit:
            found[ing] = hit
    return found


def candidate_words(query: str, cfg: Config, store: dict) -> list[str]:
    """Words that might be drug names and are not already known: letters only, 4+ characters,
    not in the office's not-a-drug list, not a section word."""
    section_words = {w for v in cfg.sections.values() for w in v["ask"] if " " not in w}
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", query):
        lw = w.lower()
        if lw in cfg.not_a_drug or lw in section_words or lw in (store.get("lookups") or {}):
            continue
        if lw not in out:
            out.append(lw)
    return out


def resolve(query: str, cfg: Config, store: dict) -> tuple[list[str], list[str]]:
    """(ingredients the question names, in order; words looked up and found not to be drugs).
    Looks a word up in RxNorm only if fetch is on_demand; results are remembered in the store."""
    found = list(known_drugs(query, store))
    not_drugs = []
    if cfg.fetch == "never":
        return found[: cfg.max_drugs], not_drugs
    words = store.setdefault("lookups", {})  # word -> ingredients ([] = RxNorm had no such drug)
    for w in candidate_words(query, cfg, store):
        if len(found) >= cfg.max_drugs:
            break
        if w in words:  # looked up before
            for ing in words[w]:
                if ing not in found:
                    found.append(ing)
            continue
        try:
            _, ings = fda.rxnorm(w, strict=True)
        except (OSError, ValueError, KeyError):
            continue  # offline: this word stays unknown
        words[w] = ings
        if not ings:
            not_drugs.append(w)
        for ing in ings:
            if ing not in found:
                found.append(ing)
    return found[: cfg.max_drugs], not_drugs


def ensure_labels(ingredients: list[str], cfg: Config, store: dict) -> list[str]:
    """Fetch labels the store lacks (on_demand). Returns the ingredients fetched now."""
    fetched = []
    if cfg.fetch == "never":
        return fetched
    for ing in ingredients:
        if ing in store["ingredients"]:
            continue
        try:
            lab = fda.label(ing, list(cfg.sections))
        except (OSError, ValueError, KeyError):
            continue
        if lab:
            lab["downloaded"] = date.today().isoformat()
            store["ingredients"][ing] = lab
            fetched.append(ing)
    return fetched


# --- the answer -------------------------------------------------------------------------------------


def label_id(lab: dict) -> str:
    return "lbl-" + lab["set_id"].replace("-", "")[:8]


def _date(t: str | None) -> str:
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if t and len(t) >= 8 else (t or "unknown")


def section_text(lab: dict, key: str, max_chars: int) -> str | None:
    parts = lab.get("sections", {}).get(key)
    if not parts:
        return None
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if len(text) > max_chars:
        cut = text.rfind(". ", 0, max_chars)
        text = text[: cut + 1 if cut > max_chars // 2 else max_chars] + " … (cut; the full section is on DailyMed)"
    return text


def pair_mentions(a: str, b: str, cfg: Config, store: dict) -> list[dict]:
    """Where a's label mentions b, using the medication review's matching on this store."""
    from mcp_servers.medication_review import server as mr

    fake = type("C", (), {"labels": store, "sections": tuple(cfg.sections), "class_terms": cfg.class_terms or {}})()
    return mr.mentions(a, b, fake)


def reference(query: str, cfg: Config | None, store: dict | None = None) -> dict:
    """The tool: {results, answer} and, when labels were fetched, "fetched" for the audit."""
    if cfg is None:
        return {"results": [], "note": "drug reference is not set up for this profile"}
    if MASK_TOKEN.search(query):
        return {
            "results": [],
            "answer": "The drug reference is about medications, not patients, so I did not look "
            "anything up for a named person. For a patient's own medications, ask for "
            "the medication review or the chart brief, which read the record. (If you "
            "named a medication and no person, it was taken for a name: an "
            "administrator can add it to privacy/drug_names.txt, or run "
            "scripts/fetch_drug_names.py to load every name RxNorm knows.)",
        }
    store = store if store is not None else load_store(cfg)
    ingredients, not_drugs = resolve(query, cfg, store)
    if not ingredients:
        hint = f" ({', '.join(not_drugs)} did not match a medication name)" if not_drugs else ""
        return {
            "results": [],
            "answer": "I couldn't tell which medication to look up"
            + hint
            + ". Please name the drug, for example: what does the label say about apixaban?",
        }
    fetched = ensure_labels(ingredients, cfg, store)
    if fetched:
        save_store(cfg, store)
    asked = sections_asked(query, cfg)
    results, lines, missing = [], [], []
    for ing in ingredients:
        lab = store["ingredients"].get(ing)
        if not lab:
            missing.append(ing)
            continue
        lid = label_id(lab)
        brands = f" ({', '.join(lab['brand_names'][:3])})" if lab.get("brand_names") else ""
        results.append(
            {
                "id": lid,
                "title": f"FDA label: {lab.get('title', ing)}",
                "text": f"{lab.get('title', ing)}, label version {_date(lab.get('effective_time'))}, "
                f"{DAILYMED.format(set_id=lab['set_id'])}",
                "data_classes": [],
            }
        )
        lines += [
            f"FDA label for {lab.get('title', ing.title())}{brands}, version {_date(lab.get('effective_time'))}"
            f"{', fetched today' if ing in fetched else ''} [{lid}]. The label's own words, quoted; not advice."
        ]
        shown = 0
        for key in asked:
            text = section_text(lab, key, cfg.max_chars)
            if text:
                lines += ["", f"{cfg.sections[key]['title']}:", f'"{text}"']
                shown += 1
        if not shown:
            lines += [
                "",
                f"This label has no {', '.join(cfg.sections[k]['title'].lower() for k in asked)} section on file. "
                "Sections on file: "
                + ", ".join(cfg.sections[k]["title"].lower() for k in lab.get("sections", {}) if k in cfg.sections)
                + ".",
            ]
        lines += ["", f"Full label: {DAILYMED.format(set_id=lab['set_id'])}", ""]
    if len(ingredients) == 2 and all(i in store["ingredients"] for i in ingredients):
        a, b = ingredients
        hits = [(a, b, h) for h in pair_mentions(a, b, cfg, store)] + [(b, a, h) for h in pair_mentions(b, a, cfg, store)]
        lines.append(f"{a.title()} and {b.title()} together:")
        if hits:
            for x, y, h in hits:
                lines.append(
                    f"- {x.title()} label, {cfg.sections.get(h['section'], {}).get('title', h['section'])} "
                    f'[{label_id(store["ingredients"][x])}], mentions "{h["term"]}": "{h["quote"]}"'
                )
        else:
            lines.append("- Neither label mentions the other in the sections on file. That does not mean they are safe together.")
        lines.append("")
    if missing:
        lines.append(
            f"No FDA prescription label was found for: {', '.join(missing)}"
            + (" (not fetched: this profile only uses labels already on file)" if cfg.fetch == "never" else "")
            + "."
        )
    if "dosage_and_administration" not in cfg.sections and re.search(r"\b(dos(e|es|ing|age)|how much|how many)\b", query, re.I):
        lines.append("Dosage is not quoted here: a dose is the prescriber's decision for a specific patient.")
    lines.append(
        "Source: FDA drug labels (openFDA), resolved through RxNorm (NLM). Labels may be out of date or "
        "incomplete; this is reference text for a clinician's judgment, not a recommendation."
    )
    out = {"results": results, "answer": "\n".join(lines).strip()}
    if fetched:
        out["fetched"] = [
            {
                "ingredient": i,
                "set_id": store["ingredients"][i]["set_id"],
                "effective_time": store["ingredients"][i].get("effective_time"),
            }
            for i in fetched
        ]
    return out


if __name__ == "__main__":
    from mcp.server.fastmcp import FastMCP

    PROFILE = load_profile()
    try:
        CONFIG = load_config(PROFILE)
    except ValueError as e:
        sys.exit(f"drug reference: {e}")
    mcp = FastMCP("drug_reference", host="127.0.0.1", port=8107)

    @mcp.tool()
    def drug_reference(query: str) -> dict:
        """What the FDA label says about the medication(s) named in the question: the asked-for
        sections quoted with the label's version date and DailyMed link. Drugs only, never a
        patient; no model, no recommendation."""
        return reference(query, CONFIG)

    if CONFIG is None:
        print(f"Drug reference for profile {PROFILE.name}: not set up (no drug_reference.yaml)")
    else:
        n = len(load_store(CONFIG).get("ingredients") or {})
        print(
            f"Drug reference for profile {PROFILE.name}: {n} label(s) on file, fetch {CONFIG.fetch}, "
            f"{len(CONFIG.sections)} quotable section(s)"
        )
    mcp.run(transport="streamable-http")
