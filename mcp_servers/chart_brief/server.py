"""MCP server: a chart brief for one patient (ADR 023).

What a clinician asked for after the first demo: before rounds, the chart put together, and
checked. The brief is the record's own lines, grouped (problem list, current medications with the
reason recorded for each, allergies, latest vitals, recent visits), every line citing the record
it came from, followed by documentation checks:

  - a medication with no recorded reason, and nothing on the problem list that the office's list
    (chart_brief.yaml) gives as a usual reason for it;
  - a problem on the list with none of the medications the office's list expects for it;
  - a vital sign outside the office's thresholds;
  - the medication review's count of FDA label mentions (ADR 017), if that is set up.

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
from pathlib import Path

import yaml

from mcp_servers.common.patients import NotFound, Source
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
    vitals: dict[str, dict]  # vital sign -> {min, max, units}
    interactions: bool
    data_classes: tuple[str, ...] = field(default=())


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
    for sec in cfg.sections:
        r = sec.record
        if r.data_class not in allowed:
            continue
        records[r.name] = src.records(r, found, allowed)
        spec = cfg.fields.get(r.name)
        rows[r.name] = src.rows(r, found, spec) if spec else []
        if src.kind == "fhir" and r.name == "vitals" and spec:
            rows[r.name] = split_components(rows[r.name], spec)
    return {"patient": patient, "name": found.name, "records": records, "rows": rows, "unread": dict(src.unread)}


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


def check_medication_reasons(meds: list[dict], problems: list[dict], cfg: Config) -> list[str]:
    out = []
    names = [p.get("name") or "" for p in problems]
    for m in meds:
        if (m.get("reason") or "").strip():
            continue
        key = _has(m.get("name"), list(cfg.medication_reason))
        if key is None:
            out.append(f"No reason recorded for {m['name']} [{m['_id']}].")
            continue
        documented = next((n for n in names if _has(n, cfg.medication_reason[key])), None)
        if documented:
            continue  # the problem list documents it, even though the field is empty
        out.append(
            f"No reason recorded for {m['name']} [{m['_id']}], and the problem list has none of the usual "
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


def check_vitals(latest: list[dict], cfg: Config) -> tuple[list[str], dict[tuple[str, str], str]]:
    """(check lines, {(record id, kind): short flag for the line in the vitals section})."""
    out, flags = [], {}
    for v in latest:
        th = cfg.vitals.get((v.get("kind") or "").lower())
        if not th or v.get("value") is None:
            continue
        try:
            x = float(v["value"])
        except (TypeError, ValueError):
            continue
        if "max" in th and x > float(th["max"]):
            flags[_key(v)] = f"above the office threshold ({th['max']})"
        elif "min" in th and x < float(th["min"]):
            flags[_key(v)] = f"below the office threshold ({th['min']})"
        else:
            continue
        out.append(
            f"{v['kind']} {x:g} {v.get('units') or ''} on {v.get('date') or 'an unknown date'} [{v['_id']}], {flags[_key(v)]}."
        )
    return out, flags


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

    meds, problems = rows.get("medications", []), rows.get("problems", [])
    latest = latest_vitals(rows.get("vitals", []))
    vital_lines, vital_flags = check_vitals(latest, cfg) if "vitals" in rows else ([], {})
    checks = []
    if "medications" in rows and "problems" in rows:
        checks += check_medication_reasons(meds, problems, cfg)
        checks += check_problem_treatment(problems, meds, cfg)
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
        for rec in recs:
            text = _strip_subject(rec["text"], name).rstrip(".")
            lines.append(_cite(text[:1].upper() + text[1:], rec["id"]))
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
            "has a medication the office expects; the latest vitals are within the office's thresholds."
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
    mcp.run(transport="streamable-http")
