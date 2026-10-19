"""MCP server: medication review for a clinician, from FDA drug labels (ADR 017).

For the patient named in the question it reads the current medication list from the records
database, and for every pair of those medications looks for where one drug's FDA label
mentions the other (by ingredient, brand name or drug class) in its contraindications or
drug-interaction section. Each finding quotes the label word for word, with the label's
source ID, date and DailyMed link.

What it does not do, by design:
- It does not decide or recommend anything: no "safe", no dose, no change. The answer says the
  decision is the clinician's. A pair the labels do not mention is reported as "not mentioned",
  never as safe.
- It uses no AI model. The facts are the label text, matched by code, so the clinician can see
  the basis of each point and judge it independently.
- It needs no network when it runs. The label sections were downloaded once, from openFDA, for
  the drugs in the records database (scripts/fetch_drug_labels.py): only drug names were sent,
  never anything about a patient. The file says when it was downloaded.

Settings come from the profile's medication_review.yaml. The tool is granted to clinicians only
(roles.yaml); the gateway also checks the role may see medication records.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from profiles.loader import Profile, load_profile
from profiles.records_map import RecordType

DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
QUOTE_CHARS = 320                      # around the mention; the full section is on DailyMed


@dataclass(frozen=True)
class Config:
    labels: dict                       # the downloaded label file (see scripts/fetch_drug_labels.py)
    medications: RecordType            # the record type holding current medications (records.yaml)
    drug_column: str                   # the column with the medication's name
    sections: tuple[str, ...]          # label sections searched, in order
    class_terms: dict[str, list[str]]  # FDA drug class -> other words labels use for it
    data_class: str                    # the data class of everything returned


def load_config(profile: Profile) -> Config | None:
    path = profile.path / "medication_review.yaml"
    if not path.exists() or profile.records_map is None:
        return None
    raw = yaml.safe_load(path.read_text()) or {}
    rtype = next((r for r in profile.records_map.records if r.name == raw["medications"]), None)
    if rtype is None:
        raise ValueError(f"{path.name}: medications '{raw['medications']}' is not a record type in records.yaml")
    labels_path = (profile.path / raw["labels"]).resolve()
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    return Config(labels, rtype, raw["drug_column"], tuple(raw.get("sections", ["contraindications", "drug_interactions"])),
                  {k.lower(): v for k, v in (raw.get("class_terms") or {}).items()}, rtype.data_class)


def ingredients_of(description: str, labels: dict) -> list[str]:
    """The ingredients of a medication: from the RxNorm lookup saved with the labels, else any
    ingredient with a label whose name appears in the description."""
    known = (labels.get("medications") or {}).get(description)
    if known and known.get("ingredients"):
        return [i.lower() for i in known["ingredients"]]
    d = description.lower()
    return sorted(i for i in (labels.get("ingredients") or {}) if re.search(rf"\b{re.escape(i)}\b", d))


def terms_for(ingredient: str, labels: dict, class_terms: dict[str, list[str]]) -> list[str]:
    """Words a label may use for this ingredient: its name, brand names, FDA drug classes and
    the other words for those classes (medication_review.yaml)."""
    lab = (labels.get("ingredients") or {}).get(ingredient) or {}
    names = [ingredient, *lab.get("brand_names", [])]          # the drug's own names first
    classes = []
    for c in lab.get("classes", []):
        classes += [c, *class_terms.get(c.lower(), [])]
    out, seen = [], set()
    for group in (names, classes):                              # within a group, longer words first
        for t in sorted({t.strip() for t in group if t and len(t.strip()) > 2}, key=len, reverse=True):
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out


def quote(text: str, start: int, end: int) -> str:
    """The sentence around a mention, cut to QUOTE_CHARS, marked with … where it was cut."""
    text = re.sub(r"\s+", " ", text)
    s = max(text.rfind(". ", 0, start), text.rfind("; ", 0, start))
    s = 0 if s < 0 else s + 2
    e = min([i for i in (text.find(". ", end), text.find("; ", end)) if i >= 0] or [len(text) - 1]) + 1
    if e - s > QUOTE_CHARS:
        s2 = max(s, start - QUOTE_CHARS // 2)
        e2 = min(e, s2 + QUOTE_CHARS)
        if s2 > s:                                   # start and end on whole words
            s2 = text.find(" ", s2, start) + 1 or s2
        if e2 < e:
            e2 = text.rfind(" ", end, e2) if text.rfind(" ", end, e2) > end else e2
        return ("…" if s2 > s else "") + text[s2:e2].strip(" ,;:") + ("…" if e2 < e else "")
    return text[s:e].strip()


def mentions(ingredient: str, other: str, cfg: Config) -> list[dict]:
    """Where `ingredient`'s label mentions `other`: one per section, the first mention."""
    lab = (cfg.labels.get("ingredients") or {}).get(ingredient)
    if not lab:
        return []
    found = []
    for section in cfg.sections:
        text = re.sub(r"\s+", " ", " ".join(lab.get("sections", {}).get(section, [])))
        for term in terms_for(other, cfg.labels, cfg.class_terms):
            m = re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
            if m:
                found.append({"section": section, "term": m.group(0), "quote": quote(text, m.start(), m.end())})
                break
    return found


def label_id(ingredient: str, labels: dict) -> str:
    return "lbl-" + labels["ingredients"][ingredient]["set_id"].replace("-", "")[:8]


def find_patient(db: sqlite3.Connection, profile: Profile, query: str):
    from mcp_servers.records.server import find_subjects
    found = find_subjects(db, profile.records_map, query)
    return found[0] if len(found) == 1 else (None, "several" if found else None)


def current_medications(db: sqlite3.Connection, cfg: Config, sid: str) -> list[dict]:
    r = cfg.medications
    cols = sorted({r.source_id.column, cfg.drug_column})
    where = " AND ".join([f'"{r.link}" = ?'] + [f'"{c}" IS NULL' if v is None else f'"{c}" = ?'
                                               for c, v in r.only_where])
    params = [sid] + [v for _, v in r.only_where if v is not None]
    rows = db.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{r.table}" WHERE {where}',
                      params).fetchall()
    meds = {}
    for row in rows:
        row = dict(zip(cols, row))
        meds.setdefault(row[cfg.drug_column], r.source_id.make(row[r.source_id.column]))   # one per drug
    return [{"id": i, "name": n} for n, i in meds.items()]


def review(db_path: Path | None, profile: Profile, cfg: Config | None, query: str, allowed_classes: list[str]) -> dict:
    """The review for the patient named in the query: results to cite, and the answer text."""
    if cfg is None or db_path is None or not Path(db_path).exists():
        return {"results": [], "note": "medication review is not set up for this profile"}
    if cfg.data_class not in allowed_classes:
        return {"results": [], "note": "this role may not see medication records"}
    if not cfg.labels.get("ingredients"):
        return {"results": [], "answer": "Medication review is not available: the FDA label file has not been "
                                         "downloaded yet (scripts/fetch_drug_labels.py)."}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sid, name = find_patient(db, profile, query)
        if sid is None:
            msg = ("More than one patient matches; please use the full name." if name == "several"
                   else "I couldn't find that patient. Please use the full name as it appears in the record.")
            return {"results": [], "answer": msg}
        meds = current_medications(db, cfg, sid)
    finally:
        db.close()
    dc = [cfg.data_class]
    results = [{"id": m["id"], "title": "Current medication", "text": f"{name}: {m['name']}", "data_classes": dc}
               for m in meds]
    for m in meds:
        m["ingredients"] = ingredients_of(m["name"], cfg.labels)

    findings, unchecked, labels_used = [], set(), {}
    for i, a in enumerate(meds):
        for b in meds[i + 1:]:
            pair = []
            for x, y in [(ia, ib) for ia in a["ingredients"] for ib in b["ingredients"]] + \
                        [(ib, ia) for ia in a["ingredients"] for ib in b["ingredients"]]:
                if x == y:
                    continue
                for hit in mentions(x, y, cfg):
                    lid = label_id(x, cfg.labels)
                    labels_used[x] = lid
                    pair.append({**hit, "label_of": x, "about": y, "id": lid})
            if pair:
                findings.append((a, b, pair))
    for m in meds:
        unchecked |= {ing for ing in m["ingredients"] if ing not in cfg.labels["ingredients"]}
    no_ingredients = [m["name"] for m in meds if not m["ingredients"]]

    for ing, lid in labels_used.items():
        lab = cfg.labels["ingredients"][ing]
        results.append({"id": lid, "title": f"FDA label: {lab.get('title', ing)}",
                        "text": f"{lab.get('title', ing)}, label version {_date(lab.get('effective_time'))}, "
                                f"{DAILYMED.format(set_id=lab['set_id'])}", "data_classes": dc})
    return {"results": results, "answer": _answer(name, meds, findings, labels_used, unchecked, no_ingredients, cfg)}


def _date(t: str | None) -> str:
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if t and len(t) >= 8 else (t or "unknown")


SECTION_NAMES = {"contraindications": "Contraindications", "drug_interactions": "Drug interactions",
                 "precautions": "Precautions", "boxed_warning": "Boxed warning", "warnings_and_cautions": "Warnings"}


def _answer(name, meds, findings, labels_used, unchecked, no_ingredients, cfg: Config) -> str:
    lines = [f"Medication review for {name}, for your review. This is not a recommendation: "
             "the decision stays with you.", "",
             "Current medications: " + "; ".join(f"{m['name']} [{m['id']}]" for m in meds) + "."]
    with_label = [m for m in meds if any(i in cfg.labels["ingredients"] for i in m["ingredients"])]
    if len(meds) < 2:
        lines += ["", "There is only one current medication, so there is no pair to check."]
    elif not with_label:
        lines += ["", "None of these medications has an FDA label on file, so nothing could be checked."]
    elif findings:
        lines += ["", "Where the FDA labels mention one of these medications with another:"]
        for n, (a, b, pair) in enumerate(findings, 1):
            lines.append(f"{n}. {a['name']} [{a['id']}] and {b['name']} [{b['id']}]")
            for h in pair:
                lab = cfg.labels["ingredients"][h["label_of"]]
                lines.append(f"   {h['label_of'].capitalize()} label, {SECTION_NAMES.get(h['section'], h['section'])} "
                             f"[{h['id']}], mentions \"{h['term']}\": \"{h['quote']}\"")
        lines += ["", "Pairs not listed above are not mentioned in these labels. That does not mean they are safe "
                      "together."]
    else:
        lines += ["", "These medications are not mentioned in each other's FDA labels on file (" +
                  ", ".join(SECTION_NAMES.get(x, x).lower() for x in cfg.sections) +
                  "). That does not mean they are safe together."]
    if unchecked or no_ingredients:
        lines += ["", "Not checked, no FDA label on file: " + ", ".join(sorted(unchecked) + no_ingredients) + "."]
    lines += ["", f"Source: FDA drug labels (openFDA), downloaded {cfg.labels.get('downloaded', 'unknown')}; "
                  "full labels on DailyMed. Labels may not list every interaction, and they are not a substitute "
                  "for your judgment or a pharmacist's review."]
    return "\n".join(lines)


if __name__ == "__main__":
    PROFILE = load_profile()
    CONFIG = load_config(PROFILE)
    mcp = FastMCP("medication_review", host="127.0.0.1", port=8103)

    @mcp.tool()
    def medication_review(query: str, allowed_classes: list[str]) -> dict:
        """For the patient named in the question: their current medications, and where the FDA
        labels mention one of them with another, quoted with the source. For a clinician's review."""
        return review(PROFILE.records_db, PROFILE, CONFIG, query, allowed_classes)

    if CONFIG is None:
        print(f"Medication review for profile {PROFILE.name}: not set up (no medication_review.yaml)")
    else:
        n = len(CONFIG.labels.get("ingredients") or {})
        print(f"Medication review for profile {PROFILE.name}: {n} FDA labels"
              + (f", downloaded {CONFIG.labels.get('downloaded')}" if n else
                 " (none yet: run scripts/fetch_drug_labels.py)"))
    mcp.run(transport="streamable-http")
