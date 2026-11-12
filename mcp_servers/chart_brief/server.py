"""MCP server: a chart brief for one patient (ADR 023).

What a clinician asked for after the first demo: before rounds, the chart put together, and
checked. The brief is the record's own lines, grouped (problem list, current medications with the
reason recorded for each, allergies, latest vitals, recent visits), every line citing the record
it came from, followed by documentation checks:

  - a medication with no recorded reason, and nothing on the problem list that the office's list
    (chart_brief.yaml) gives as a usual reason for it;
  - a problem on the list with none of the medications the office's list expects for it;
  - a vital sign outside the office's thresholds, outside the range a measurement could be, or dated
    in the future;
  - the same medication in several current orders, and the same ingredient in several medications;
  - the medication review's count of FDA label mentions (ADR 017), if that is set up.

Entries the record repeats (the same medication ordered many times) are shown once, with how many
there are and the dates they span.

No model writes a fact. The tool's text is the answer (tools.yaml answer: direct), so a model
cannot turn a list into advice. The checks compare fields of the record with lists the office
wrote; they are worded as what the record does or does not show, never as what to do. Accuracy
is therefore the record's accuracy, and it is tested, not estimated (eval/chart_brief_accuracy.py).

Records come from the profile's records source: the database (records.yaml) or a FHIR server
(fhir.yaml, ADR 013). Only the data classes the caller's role may see are read; a section whose
class the role may not see is left out and said so. This is the only kind of tool that receives
the real question (receives_identifiers): it looks the patient up by name.
"""

import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from mcp_servers.common.patients import NotFound, Source
from mcp_servers.common.serve import serve
from profiles.loader import Profile, load_profile


@dataclass(frozen=True)
class Section:
    record: object  # a RecordType of records.yaml or fhir.yaml
    heading: str


@dataclass(frozen=True)
class Config:
    source: str  # db or fhir
    sections: tuple[Section, ...]
    fields: dict[str, dict[str, str]]  # section name -> field -> column or FHIR path
    medication_reason: dict[str, list[str]]  # ingredient/class word -> problem terms
    problem_treatment: dict[str, list[str]]  # problem term -> medication terms
    vitals: dict[str, dict]  # vital sign -> {min, max, units, names, plausible}
    interactions: bool
    data_classes: tuple[str, ...] = field(default=())
    problem_not: dict[str, list[str]] = field(default_factory=dict)  # problem term -> words that make it another problem
    duplicates: bool = True  # report repeated orders and an ingredient in several medications
    complete: tuple[str, ...] = ()  # sections that must be whole: a cut-off list is said so, in place and as a check


def load_config(profile: Profile) -> Config | None:
    path = profile.path / "chart_brief.yaml"
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = raw.get("source", "db")
    if source not in ("db", "fhir"):
        raise ValueError(f"{path.name}: source must be db or fhir")
    mapping = profile.fhir_map if source == "fhir" else profile.records_map
    if mapping is None:
        raise ValueError(
            f"{path.name}: source {source} but the profile has no {'fhir.yaml' if source == 'fhir' else 'records.yaml'}"
        )
    sections = []
    for s in raw.get("sections") or []:
        rt = next((r for r in mapping.records if r.name == s["record"]), None)
        if rt is None:
            raise ValueError(f"{path.name}: section record '{s['record']}' is not a record type")
        sections.append(Section(rt, str(s.get("heading") or s["record"])))
    if not sections:
        raise ValueError(f"{path.name}: sections is empty")
    checks = raw.get("checks") or {}
    low = lambda d: {str(k).lower(): [str(t).lower() for t in v] for k, v in (d or {}).items()}  # noqa: E731
    fields = {k: {f: str(v) for f, v in (spec or {}).items()} for k, spec in (raw.get("fields") or {}).items()}
    for name in ("medications", "problems", "vitals"):
        if name in {s.record.name for s in sections} and name not in fields:
            raise ValueError(f"{path.name}: fields.{name} is needed for the checks")
    return Config(
        source,
        tuple(sections),
        fields,
        low(checks.get("medication_reason")),
        low(checks.get("problem_treatment")),
        {str(k).lower(): dict(v) for k, v in (checks.get("vitals") or {}).items()},
        bool(checks.get("interactions", False)),
        tuple(dict.fromkeys(s.record.data_class for s in sections)),
        low(checks.get("problem_not")),
        bool(checks.get("duplicates", True)),
        tuple(raw.get("complete", [s.record.name for s in sections])),
    )


# --- reading the record: cited lines and structured rows, from either source ---------------------


def read(src: Source, cfg: Config, query: str, allowed: set[str]) -> dict:
    """{patient, name, records: {section: [cited lines]}, rows: {section: [structured rows]}, unread}
    or {"error": reply}. One reader for both sources (mcp_servers/common/patients.py)."""
    found = src.find(query)
    if isinstance(found, NotFound):
        return {"error": found.reply}
    patient = {"id": found.cited_id, "text": found.text} if src.subject_class in allowed else None
    records, rows = {}, {}
    src.prefetch([sec.record for sec in cfg.sections if sec.record.data_class in allowed], found)
    for sec in cfg.sections:
        r = sec.record
        if r.data_class not in allowed:
            continue
        records[r.name] = src.records(r, found, allowed)
        spec = cfg.fields.get(r.name)
        rows[r.name] = src.rows(r, found, spec) if spec else []
        if src.kind == "fhir" and r.name == "vitals" and spec:
            rows[r.name] = split_components(rows[r.name], spec)
    return {"patient": patient, "name": found.name, "records": records, "rows": rows, "unread": dict(src.unread),
            "cut": dict(src.cut)}


def split_components(rows: list[dict], spec: dict) -> list[dict]:
    """FHIR blood pressure is one Observation with two components: one row per component."""
    from profiles.fhir_map import value_of

    out = []
    for row in rows:
        res = row.get("_resource") or {}
        if not res.get("component"):
            out.append({k: v for k, v in row.items() if k != "_resource"})
            continue
        for comp in res["component"]:
            out.append({
                "kind": value_of(comp, "code.text,code.coding.display", {}),
                "value": value_of(comp, "valueQuantity.value", {}),
                "units": value_of(comp, "valueQuantity.unit", {}),
                "date": value_of(res, spec.get("date", "effectiveDateTime|date"), {}),
                "_id": row["_id"],
            })
    return out


# --- the checks: fields of the record against the office's lists --------------------------------


def _has(text: str | None, terms: list[str]) -> str | None:
    t = (text or "").lower()
    return next((term for term in terms if term in t), None)


def _names(problem: str | None, terms: list[str], cfg: Config) -> bool:
    """The problem is one of the terms, and not a different problem that shares the word
    (problem_not: chronic venous hypertension is not hypertension)."""
    term = _has(problem, terms)
    return term is not None and not _has(problem, cfg.problem_not.get(term, []))


def _cap(text: str | None) -> str:
    t = text or ""
    return t[:1].upper() + t[1:]


def check_medication_reasons(meds: list[dict], problems: list[dict], cfg: Config) -> list[str]:
    out = []
    names = [p.get("name") or "" for p in problems]
    for m in meds:
        if (m.get("reason") or "").strip():
            continue
        key = _has(m.get("name"), list(cfg.medication_reason))
        orders = f" in any of its {m['_count']} orders" if m.get("_count", 1) > 1 else ""
        if key is None:
            out.append(f"No reason recorded for {_cap(m['name'])}{orders} [{m['_id']}].")
            continue
        documented = next((n for n in names if _names(n, cfg.medication_reason[key], cfg)), None)
        if documented:
            continue  # the problem list documents it, even though the field is empty
        out.append(
            f"No reason recorded for {_cap(m['name'])}{orders} [{m['_id']}], and the problem list has none of the usual "
            f"reasons the office lists for {key} ({', '.join(cfg.medication_reason[key])})."
        )
    return out


def check_problem_treatment(problems: list[dict], meds: list[dict], cfg: Config) -> list[str]:
    out = []
    med_names = " | ".join((m.get("name") or "").lower() for m in meds)
    for p in problems:
        key = _has(p.get("name"), list(cfg.problem_treatment))
        if key is None:
            continue
        if _has(p.get("name"), cfg.problem_not.get(key, [])):   # venous hypertension is not hypertension
            continue
        if any(term in med_names for term in cfg.problem_treatment[key]):
            continue
        out.append(
            f"{p['name']} [{p['_id']}] is on the problem list, and none of the medications the office lists "
            f"for {key} ({', '.join(cfg.problem_treatment[key][:6])}{'…' if len(cfg.problem_treatment[key]) > 6 else ''}) "
            "is on the current list."
        )
    return out


def latest_vitals(vitals: list[dict]) -> list[dict]:
    """The most recent value of each kind, in the record's order."""
    seen, out = set(), []
    for v in sorted(vitals, key=lambda v: str(v.get("date") or ""), reverse=True):
        kind = (v.get("kind") or "").lower()
        if kind and kind not in seen:
            seen.add(kind)
            out.append(v)
    return out


def _key(v: dict) -> tuple[str, str]:
    return v["_id"], (v.get("kind") or "").lower()  # blood pressure: two kinds in one FHIR resource


UNITS = {"cel": "C", "degc": "C", "°c": "C", "c": "C", "[degf]": "F", "degf": "F", "°f": "F", "f": "F",
         "/min": "/min", "bpm": "/min", "beats/min": "/min", "{beats}/min": "/min", "breaths/min": "/min",
         "%": "%", "mmhg": "mmHg", "mm[hg]": "mmHg"}


def _units(u) -> str | None:
    u = str(u or "").strip().lower()
    return UNITS.get(u, u) or None


def threshold_for(kind: str | None, cfg: Config) -> dict | None:
    """The office's entry for a vital sign: by its name, else by one of the entry's `names` (words
    the record may use instead, e.g. Oracle Health's "Temperature Oral"), longest word first."""
    k = (kind or "").lower()
    if k in cfg.vitals:
        return cfg.vitals[k]
    pairs = [(w.lower(), th) for th in cfg.vitals.values() for w in th.get("names", [])]
    return next((th for w, th in sorted(pairs, key=lambda x: -len(x[0])) if w in k), None)


def check_vitals(latest: list[dict], cfg: Config, today: str | None = None) -> tuple[list[str], dict[tuple[str, str], str]]:
    """(check lines, {(record id, kind): short flag for the line in the vitals section}).
    A value is compared only when its units are the office's units (no conversion: 98.6 F is not
    compared with a Celsius threshold). A value outside `plausible` is reported as one a measurement
    could not be, and a date after today as a future date: both are worth checking in the record."""
    today = today or date.today().isoformat()
    out, flags = [], {}
    for v in latest:
        notes = []
        when = str(v.get("date") or "")
        if when and when[:10] > today:
            notes.append(f"dated in the future ({when[:10]})")
        th = threshold_for(v.get("kind"), cfg)
        x = None
        try:
            x = float(v["value"]) if v.get("value") is not None else None
        except (TypeError, ValueError):
            pass
        if th and x is not None:
            same = not th.get("units") or not v.get("units") or _units(th["units"]) == _units(v["units"])
            pl = th.get("plausible") or {}
            if not same:
                pass
            elif ("min" in pl and x < float(pl["min"])) or ("max" in pl and x > float(pl["max"])):
                notes.append(f"outside the range a measurement could be ({pl.get('min', '')}–{pl.get('max', '')}); "
                             "check the record")
            elif "max" in th and x > float(th["max"]):
                notes.append(f"above the office threshold ({th['max']})")
            elif "min" in th and x < float(th["min"]):
                notes.append(f"below the office threshold ({th['min']})")
        if not notes:
            continue
        flags[_key(v)] = "; ".join(notes)
        shown = f"{x:g}" if x is not None else str(v.get("value"))
        out.append(f"{v['kind']} {shown} {v.get('units') or ''} on {v.get('date') or 'an unknown date'} "
                   f"[{v['_id']}], {flags[_key(v)]}.")
    return out, flags


# --- repeated entries --------------------------------------------------------------------------------

DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def group_medications(meds: list[dict]) -> list[dict]:
    """One row per medication name, in the record's order: the first row, with `_count` (how many
    current orders name it) and `_ids` (all their cited ids)."""
    out: dict[str, dict] = {}
    for m in meds:
        key = (m.get("name") or "").strip().lower()
        if not key:
            continue
        if key in out:
            out[key]["_count"] += 1
            out[key]["_ids"].append(m["_id"])
            if (m.get("reason") or "").strip() and not (out[key].get("reason") or "").strip():
                out[key]["reason"] = m["reason"]
        else:
            out[key] = {**m, "_count": 1, "_ids": [m["_id"]]}
    return list(out.values())


def _ids(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else f"{ids[0]} … {ids[-1]}" if len(ids) > 3 else ", ".join(ids)


def check_duplicates(meds: list[dict], ingredients_of=None) -> list[str]:
    """The same medication in several current orders; the same ingredient in several medications
    (acetaminophen in Tylenol and in Tylenol PM). Facts of the record, not a judgment of the doses."""
    out = [f"{_cap(m['name'])} appears in {m['_count']} current orders [{_ids(m['_ids'])}]."
           for m in meds if m.get("_count", 1) > 1]
    if ingredients_of is None:
        return out
    by_ingredient: dict[str, list[dict]] = {}
    for m in meds:
        for i in dict.fromkeys(ingredients_of(m)):
            by_ingredient.setdefault(i, []).append(m)
    for i, ms in by_ingredient.items():
        if len(ms) > 1:
            out.append(f"{i[:1].upper() + i[1:]} is an ingredient of {len(ms)} current medications: "
                       + "; ".join(f"{_cap(m['name'])} [{m['_id']}]" for m in ms) + ".")
    return out


def _ingredient_reader(profile: Profile):
    """A function from a medication row to its ingredients, from the label file's RxNorm data
    (medication_review.yaml); None when there is no label file."""
    try:
        from mcp_servers.medication_review import server as mr
        cfg = mr.load_config(profile)
    except Exception:  # noqa: BLE001
        return None
    if cfg is None or not cfg.labels:
        return None

    def read(m: dict) -> list[str]:
        res = m.get("_resource") or {}
        return mr.ingredients_of(m.get("name") or "", cfg.labels, mr._rxcui(res.get("medicationCodeableConcept")))
    return read


def tidy(text: str) -> str:
    """Record text as a reader expects it: no doubled "Allergy to", no trailing "allergy" after it,
    and a missing date said plainly."""
    t = re.sub(r"(?i)\ballergy to allergy to\b", "Allergy to", text)
    t = re.sub(r"(?i)^unknown visit\b", "Visit", t)                 # no visit type recorded
    t = re.sub(r"(?i)^(allergy to .+?) allergy\b", r"\1", t)
    return re.sub(r",? (?:on|since|ordered|measured) unknown\b", ", date not recorded", t)


def group_lines(recs: list[dict], name: str) -> list[tuple[str, str]]:
    """(line text, cited id) per entry, entries the record repeats (the same text but for its dates)
    shown once: the latest, with how many there are and the dates they span."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for rec in recs:
        text = tidy(_strip_subject(rec["text"], name).rstrip("."))
        text = text[:1].upper() + text[1:]
        groups.setdefault(DATE.sub("", text).lower(), []).append((text, rec["id"]))
    out = []
    for entries in groups.values():
        if len(entries) == 1:
            out.append(entries[0])
            continue
        dates = sorted(d for t, _ in entries for d in DATE.findall(t))
        latest = max(entries, key=lambda e: max(DATE.findall(e[0]) or [""]))
        span = f", {dates[0]} to {dates[-1]}" if dates and dates[0] != dates[-1] else ""
        out.append((f"{latest[0]} ({len(entries)} entries in the record{span})", latest[1]))
    return out


def interactions_summary(profile: Profile, query: str, allowed: list[str], db_path, client) -> tuple[str | None, list[dict]]:
    """One line from the medication review (ADR 017), and its label records to cite."""
    try:
        from mcp_servers.medication_review import server as mr
    except Exception:  # noqa: BLE001
        return None, []
    cfg = mr.load_config(profile)
    if cfg is None or not cfg.labels.get("ingredients"):
        return None, []
    out = mr.review(db_path, profile, cfg, query, allowed, client)
    labels = [r for r in out.get("results", []) if str(r.get("id", "")).startswith("lbl-")]
    if not out.get("results"):
        return None, []
    pairs = len(re.findall(r"^\d+\. ", out.get("answer", ""), re.M))
    if pairs:
        return (
            f"Medication review: the FDA labels on file mention {pairs} pair(s) of these medications together "
            f'({", ".join(r["id"] for r in labels)}). Ask "check the medications for interactions" for the quotes.'
        ), labels
    return "Medication review: no pair of these medications is mentioned in the FDA labels on file.", labels


# --- the brief -----------------------------------------------------------------------------------


def _cite(line: str, rid: str) -> str:
    return f"- {line} [{rid}]"


def _strip_subject(text: str, name: str) -> str:
    return text[len(name) + 2 :] if text.startswith(name + ": ") else text


def brief(
    db_path: Path | None, profile: Profile, cfg: Config | None, query: str, allowed_classes: list[str], client=None
) -> dict:
    """The brief for the patient named in the query: results to cite, and the answer text."""
    src = Source.open(profile, cfg.source, db_path, client) if cfg is not None else None
    if src is None:
        return {"results": [], "note": "chart brief is not set up for this profile"}
    allowed = set(allowed_classes)
    clinical = {s.record.data_class for s in cfg.sections if s.record.name in ("problems", "medications")}
    if not any(c in allowed for c in clinical):
        return {
            "results": [],
            "answer": "A chart brief needs the problem list or the medication list, which this "
            "role may not see. A clinician or nurse can ask for it.",
        }
    data = read(src, cfg, query, allowed)
    if "error" in data:
        return {"results": [], "answer": data["error"]}
    name, records, rows, unread = data["name"], data["records"], data["rows"], data.get("unread") or {}
    cut = {k: n for k, n in (data.get("cut") or {}).items() if k in cfg.complete}
    results = (
        [
            {
                "id": data["patient"]["id"],
                "title": f"Patient: {name}",
                "text": data["patient"]["text"],
                "data_classes": [src.subject_class],
            }
        ]
        if data.get("patient")
        else []
    )
    for sec in cfg.sections:
        results += records.get(sec.record.name, [])

    meds, problems = group_medications(rows.get("medications", [])), rows.get("problems", [])
    latest = latest_vitals(rows.get("vitals", []))
    vital_lines, vital_flags = check_vitals(latest, cfg) if "vitals" in rows else ([], {})
    checks = [
        f"{sec.heading} is incomplete: {cut[sec.record.name]} entries were read and the record has more, so this "
        "brief and its checks may miss some. Check the record directly."
        for sec in cfg.sections if sec.record.name in cut and sec.record.data_class in allowed
    ]
    if "medications" in rows and "problems" in rows:
        checks += check_medication_reasons(meds, problems, cfg)
        checks += check_problem_treatment(problems, meds, cfg)
    if "medications" in rows and cfg.duplicates:
        checks += check_duplicates(meds, _ingredient_reader(profile))
    checks += vital_lines
    inter_line = None
    if cfg.interactions and "medications" in rows:
        inter_line, labels = interactions_summary(profile, query, allowed_classes, db_path, client)
        results += [lab for lab in labels if lab["id"] not in {r["id"] for r in results}]

    head = f"Chart brief for {name}"
    if data.get("patient"):
        about = data["patient"]["text"].rstrip(".")
        about = about[len(name) + 2 :] if about.startswith(name + ", ") else _strip_subject(about, name)
        head += f" ({about}) [{data['patient']['id']}]"
    lines = [
        head + ", from the record, for your review. Every line cites the record it comes from. "
        "The checks at the end are documentation checks against the office's lists, not clinical judgments."
    ]
    left_out = []
    for sec in cfg.sections:
        r = sec.record
        if r.data_class not in allowed:
            left_out.append(sec.heading.lower())
            continue
        recs = records.get(r.name, [])
        lines += ["", f"{sec.heading}:"]
        if r.name in unread:
            lines.append(f"- could not be read: the record system did not answer ({unread[r.name][:120]})")
            continue
        if not recs:
            lines.append("- none recorded")
            continue
        if r.name == "vitals" and latest:  # the latest of each kind, from the structured fields
            for v in latest:
                value = v.get("value")
                try:
                    value = f"{float(value):g}"
                except (TypeError, ValueError):
                    value = str(value)
                flag = vital_flags.get(_key(v))
                lines.append(
                    _cite(
                        f"{v.get('kind')} {value} {v.get('units') or ''}".strip()
                        + f", measured {v.get('date') or 'on an unknown date'}",
                        v["_id"],
                    )
                    + (f" — {flag}" if flag else "")
                )
            continue
        for text, rid in group_lines(recs, name):
            lines.append(_cite(text, rid))
        if r.name in cut:
            lines.append(f"- {cut[r.name]} shown; the record has more. Check it directly.")
    lines += ["", "Checks (what the record does or does not show):"]
    if checks:
        lines += [f"- {c}" for c in checks]
        if unread:
            lines.append(f"- Not checked: {', '.join(unread)} could not be read.")
    elif "medications" not in rows or "problems" not in rows:
        lines.append("- Not run: the checks need the problem list and the medication list.")
    else:
        lines.append(
            "- Every current medication has a recorded reason or a matching problem; every listed problem "
            "has a medication the office expects; no medication or ingredient is listed twice; the latest "
            "vitals are within the office's thresholds."
        )
    if inter_line:
        lines.append(f"- {inter_line}")
    if left_out:
        lines += ["", f"Not shown for this role: {', '.join(left_out)}."]
    lines += ["", "Nothing here is a recommendation. Decisions about care stay with the patient's clinician."]
    return {"results": results, "answer": "\n".join(lines)}


if __name__ == "__main__":
    from mcp.server.fastmcp import FastMCP

    PROFILE = load_profile()
    try:
        CONFIG = load_config(PROFILE)
    except ValueError as e:
        sys.exit(f"chart brief: {e}")
    CLIENT = None
    if CONFIG is not None and CONFIG.source == "fhir":
        from fhir.client import FhirError, client_from_settings

        try:
            CLIENT = client_from_settings(PROFILE.fhir_map.server)
        except FhirError as e:
            print(f"Chart brief: the FHIR server is not set up yet ({e})")
    mcp = FastMCP("chart_brief", host="127.0.0.1", port=8106)

    @mcp.tool()
    def chart_brief(query: str, allowed_classes: list[str]) -> dict:
        """For the patient named in the question: the record's problem list, current medications
        with recorded reasons, allergies, latest vitals and recent visits, cited line by line, with
        documentation checks. For a clinician's or nurse's review; no model, no recommendation."""
        return brief(PROFILE.records_db, PROFILE, CONFIG, query, allowed_classes, CLIENT)

    if CONFIG is None:
        print(f"Chart brief for profile {PROFILE.name}: not set up (no chart_brief.yaml)")
    else:
        print(
            f"Chart brief for profile {PROFILE.name}: {CONFIG.source}, {len(CONFIG.sections)} section(s), "
            f"{len(CONFIG.medication_reason)} medication reasons, {len(CONFIG.problem_treatment)} problem treatments, "
            f"{len(CONFIG.vitals)} vital thresholds"
        )
    serve(mcp)   # plain HTTP, or mutual TLS (ADR 030)
