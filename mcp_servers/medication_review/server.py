"""MCP server: medication review for a clinician, from FDA drug labels (ADR 017; Epic records: ADR 013).

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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from mcp_servers.common.patients import NotFound, Patient, Source
from mcp_servers.common.serve import serve
from privacy.sensitivity import resource_tagger
from profiles.loader import Profile, load_profile

DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
QUOTE_CHARS = 320                      # around the mention; the full section is on DailyMed


@dataclass(frozen=True)
class Config:
    labels: dict                       # the downloaded label file (see scripts/fetch_drug_labels.py)
    medications: object                # the record type holding current medications (records.yaml or fhir.yaml)
    drug_column: str | None            # database source: the column with the medication's name
    sections: tuple[str, ...]          # label sections searched, in order
    class_terms: dict[str, list[str]]  # FDA drug class -> other words labels use for it
    data_class: str                    # the data class of everything returned
    source: str = "database"           # database (records.yaml) or fhir (fhir.yaml, ADR 013)
    labels_path: Path | None = None    # where the label file lives (written to by on-demand fetches)
    labels_fetch: str = "never"        # never: only the downloaded file · on_demand: fetch what is missing


def load_config(profile: Profile) -> Config | None:
    path = profile.path / "medication_review.yaml"
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text()) or {}
    source = raw.get("source", "database")
    mapping = profile.fhir_map if source == "fhir" else profile.records_map
    if mapping is None:
        return None
    rtype = next((r for r in mapping.records if r.name == raw["medications"]), None)
    if rtype is None:
        raise ValueError(f"{path.name}: medications '{raw['medications']}' is not a record type in "
                         f"{'fhir.yaml' if source == 'fhir' else 'records.yaml'}")
    labels_path = (profile.path / raw["labels"]).resolve()
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    fetch = raw.get("labels_fetch", "never")
    if fetch not in ("never", "on_demand"):
        raise ValueError(f"{path.name}: labels_fetch must be never or on_demand, not {fetch!r}")
    return Config(labels, rtype, raw.get("drug_column"),
                  tuple(raw.get("sections", ["contraindications", "drug_interactions"])),
                  {k.lower(): v for k, v in (raw.get("class_terms") or {}).items()}, rtype.data_class, source,
                  labels_path, fetch)


def fetch_missing(cfg: Config, meds: list[dict], lookup=None) -> list[dict]:
    """With labels_fetch: on_demand, look up the ingredients and FDA labels the file lacks for these
    medications, save them to the file with today's date, and return what was fetched (for the audit
    and the answer). Drug names and codes only go out; never the patient. With "never", nothing."""
    if cfg.labels_fetch != "on_demand" or not meds:
        return []
    from mcp_servers.medication_review import labels as lk
    fetched = (lookup or lk.fetch_into)(cfg.labels, meds, list(cfg.sections))
    if fetched and cfg.labels_path is not None:
        cfg.labels.setdefault("source", "openFDA drug label API, https://open.fda.gov/apis/drug/label/ ; "
                                        "ingredients from RxNorm, https://lhncbc.nlm.nih.gov/RxNav/")
        cfg.labels.setdefault("sections", list(cfg.sections))
        cfg.labels.setdefault("downloaded", date.today().isoformat())
        cfg.labels_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.labels_path.write_text(json.dumps(cfg.labels, indent=1, sort_keys=True) + "\n")
    return fetched


def ingredients_of(description: str, labels: dict, rxcui: str | None = None) -> list[str]:
    """The ingredients of a medication: from its RxNorm code, or the RxNorm lookup saved with the
    labels, else any ingredient with a label whose name appears in the description."""
    by_code = (labels.get("rxcui") or {}).get(rxcui or "")
    if by_code:
        return [i.lower() for i in by_code]
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


RXNORM = "rxnorm"


def _rxcui(concept: dict | None) -> str | None:
    for c in (concept or {}).get("coding") or []:
        if RXNORM in (c.get("system") or "").lower() and c.get("code"):
            return str(c["code"])
    return None


def medications_of(src: Source, cfg: Config, patient: Patient) -> list[dict]:
    """The patient's current medications from either source: [{id, name[, rxcui]}], one per drug.
    On FHIR a coded medication is read for its RxNorm code; a reference's display name is enough."""
    r = cfg.medications
    meds = {}
    if src.kind == "db":
        for row in src.rows(r, patient, {"name": cfg.drug_column}):
            meds.setdefault(row["name"], {"id": row["_id"], "name": row["name"]})
        return list(meds.values())
    tag = resource_tagger(src.profile.sensitivity)   # the resource's own labels and codes (ADR 038)
    for row in src.rows(r, patient, {}):
        res = row["_resource"]
        concept = res.get("medicationCodeableConcept")
        if not concept and (res.get("medicationReference") or {}).get("reference", "").startswith("Medication/"):
            ref = res["medicationReference"]
            try:
                concept = src.client.read("Medication", ref["reference"].split("/", 1)[1]).get("code")
            except Exception:                 # noqa: BLE001  the display name is still enough to list it
                concept = {"text": ref.get("display")}
        name = (concept or {}).get("text") or (res.get("medicationReference") or {}).get("display") or \
            next((c.get("display") for c in (concept or {}).get("coding") or [] if c.get("display")), None)
        if name:
            meds.setdefault(name, {"id": row["_id"], "name": name, "rxcui": _rxcui(concept),
                                   **({"sensitivity": tag(res)} if tag else {})})
    return list(meds.values())


def medication_list(db_path: Path | None, profile: Profile, cfg: Config | None, query: str,
                    allowed_classes: list[str], client=None) -> dict:
    """The patient's current medications exactly as the record lists them, cited, with no model: a
    list question needs no AI to answer, and record text cannot add advice. The records come from the
    profile's records source (database or FHIR), asked for the medications data class only."""
    if cfg is None:
        return {"results": [], "note": "medication list is not set up for this profile"}
    if cfg.data_class not in allowed_classes:
        return {"results": [], "note": "this role may not see medication records"}
    if cfg.source == "fhir":
        if client is None:
            return {"results": [], "answer": "The record system is not set up, so nothing was looked up."}
        from mcp_servers.fhir_records.server import search as records_search
        out = records_search(client, profile.fhir_map, query, [cfg.data_class], resource_tagger(profile.sensitivity))
    else:
        from mcp_servers.records.server import search as db_search
        out = db_search(db_path, query, [cfg.data_class], profile.records_map)
    found = [r for r in out.get("results", []) if cfg.data_class in r.get("data_classes", [])]
    if not found:
        note = out.get("note")
        return {"results": [], "answer": (note[0].upper() + note[1:] + ".") if note else
                "The record lists no current medications for that patient."}
    source = "the record system" if cfg.source == "fhir" else "the clinic's records"
    lines = [f"Current medications, as written in {source}:", ""]
    lines += [f"- {r['text']} [{r['id']}]" for r in found]
    lines += ["", "This is the record's list, not a recommendation; decisions about medications stay with the "
                  "patient's clinician."]
    return {"results": found, "answer": "\n".join(lines)}


def review(db_path: Path | None, profile: Profile, cfg: Config | None, query: str, allowed_classes: list[str],
           client=None) -> dict:
    """The review for the patient named in the query: results to cite, and the answer text.
    Medications come from the records database, or with a FHIR source (ADR 013) from `client`."""
    src = Source.open(profile, "fhir" if cfg is not None and cfg.source == "fhir" else "db", db_path, client) \
        if cfg is not None else None
    if src is None:
        return {"results": [], "note": "medication review is not set up for this profile"}
    if cfg.data_class not in allowed_classes:
        return {"results": [], "note": "this role may not see medication records"}
    if not cfg.labels.get("ingredients") and cfg.labels_fetch != "on_demand":
        return {"results": [], "answer": "Medication review is not available: the FDA label file has not been "
                                         "downloaded yet (scripts/fetch_drug_labels.py)."}
    found = src.find(query)
    if isinstance(found, NotFound):
        if found.why == "unavailable":
            reply = found.reply.replace("did not answer:", "did not answer, so nothing was reviewed:", 1)
            return {"results": [], "answer": reply}
        return {"results": [], "answer": found.reply}
    name = found.name
    try:
        meds = medications_of(src, cfg, found)
    except Exception as e:                    # noqa: BLE001  FhirError or a database error
        return {"results": [], "answer": f"The record system did not answer, so nothing was reviewed: {e}"}
    if src.unread:
        return {"results": [], "answer": "The record system did not answer, so nothing was reviewed: "
                                         + "; ".join(src.unread.values())}
    dc = [cfg.data_class]
    results = [{"id": m["id"], "title": "Current medication", "text": f"{name}: {m['name']}", "data_classes": dc,
                **({"sensitivity": m["sensitivity"]} if "sensitivity" in m else {})} for m in meds]
    missing = [m for m in meds if not ingredients_of(m["name"], cfg.labels, m.get("rxcui"))
               or any(i not in (cfg.labels.get("ingredients") or {})
                      for i in ingredients_of(m["name"], cfg.labels, m.get("rxcui")))]
    fetched = fetch_missing(cfg, missing)
    if not cfg.labels.get("ingredients"):
        return {"results": [], "answer": "Medication review is not available: no FDA label could be found "
                                         "for these medications (RxNorm and openFDA were asked)."}
    for m in meds:
        m["ingredients"] = ingredients_of(m["name"], cfg.labels, m.get("rxcui"))

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
    out = {"results": results, "answer": _answer(name, meds, findings, labels_used, unchecked, no_ingredients, cfg,
                                                 fetched)}
    if fetched:       # audited with the tool call by the gateway: drug names and label IDs, never the patient
        out["fetched"] = [{"medication": f["medication"], "labels": f["labels"]} for f in fetched]
    return out


def _date(t: str | None) -> str:
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if t and len(t) >= 8 else (t or "unknown")


SECTION_NAMES = {"contraindications": "Contraindications", "drug_interactions": "Drug interactions",
                 "precautions": "Precautions", "boxed_warning": "Boxed warning", "warnings_and_cautions": "Warnings"}


def _answer(name, meds, findings, labels_used, unchecked, no_ingredients, cfg: Config, fetched=()) -> str:
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
    new = [lab for f in fetched for lab in f["labels"]]
    if new:
        lines += ["", "Fetched for this review and kept on file, dated today: " +
                  "; ".join(f"{lab['ingredient']} (label version {_date(lab.get('effective_time'))})" for lab in new) + "."]
    lines += ["", f"Source: FDA drug labels (openFDA), downloaded {cfg.labels.get('downloaded', 'unknown')}; "
                  "full labels on DailyMed. Labels may not list every interaction, and they are not a substitute "
                  "for your judgment or a pharmacist's review."]
    return "\n".join(lines)


if __name__ == "__main__":
    PROFILE = load_profile()
    CONFIG = load_config(PROFILE)
    CLIENT = None
    if CONFIG is not None and CONFIG.source == "fhir":
        from fhir.client import FhirError, client_from_settings
        try:
            CLIENT = client_from_settings(PROFILE.fhir_map.server)
        except FhirError as e:
            print(f"Medication review: the FHIR server is not set up yet ({e})")
    mcp = FastMCP("medication_review", host="127.0.0.1", port=8103)

    def _budget():
        """One time budget for all the FHIR requests of a tool call (ADR 036)."""
        from fhir.transport import request_budget
        fhir = CONFIG is not None and CONFIG.source == "fhir" and PROFILE.fhir_map is not None
        return request_budget(PROFILE.fhir_map.server if fhir else None)

    def _client(launch_ref):
        """The clinician's own client when launched from the chart (the gateway sets launch_ref)."""
        if launch_ref and PROFILE.fhir_map is not None and PROFILE.fhir_map.launch:
            from fhir.smart_launch import launch_client
            return launch_client(PROFILE.fhir_map, launch_ref, PROFILE.name)
        return CLIENT

    @mcp.tool()
    def medication_review(query: str, allowed_classes: list[str], launch_ref: str | None = None) -> dict:
        """For the patient named in the question: their current medications, and where the FDA
        labels mention one of them with another, quoted with the source. For a clinician's review."""
        with _budget():
            try:
                client = _client(launch_ref)
            except FhirError as e:
                return {"results": [], "answer": str(e)}
            return review(PROFILE.records_db, PROFILE, CONFIG, query, allowed_classes, client)

    @mcp.tool(name="medication_list")
    def medication_list_tool(query: str, allowed_classes: list[str], launch_ref: str | None = None) -> dict:
        """For the patient named in the question: their current medications exactly as the record
        lists them, with source IDs. No model, no recommendation."""
        with _budget():
            try:
                client = _client(launch_ref)
            except FhirError as e:
                return {"results": [], "answer": str(e)}
            return medication_list(PROFILE.records_db, PROFILE, CONFIG, query, allowed_classes, client)

    if CONFIG is None:
        print(f"Medication review for profile {PROFILE.name}: not set up (no medication_review.yaml)")
    else:
        n = len(CONFIG.labels.get("ingredients") or {})
        print(f"Medication review for profile {PROFILE.name}: {n} FDA labels"
              + (f", downloaded {CONFIG.labels.get('downloaded')}" if n else
                 " (none yet: run scripts/fetch_drug_labels.py)"))
    serve(mcp)   # plain HTTP, or mutual TLS (ADR 030)
