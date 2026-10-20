"""MCP server: the patient's records from a FHIR server (Epic's sandbox, or any FHIR R4 server), ADR 013.

The FHIR counterpart of mcp_servers/records: the same tool (`search_records`), the same records (id,
title, text, data classes) and the same rules, so the gateway, the roles and the answer checks work
unchanged. What it reads, and how values become text, comes from the profile's fhir.yaml.

- Read-only: the FHIR client only sends GET requests (fhir/client.py).
- Need-to-know: a resource type is requested only when the caller's role may see its data class, so
  data a role may not see is never fetched from the record system.
- The patient is found by name, from the question: given and family name, searched on the server.
  More than one match, or none, returns nothing.
"""

import re

from mcp.server.fastmcp import FastMCP

from fhir.client import FhirClient, FhirError, client_from_settings
from profiles.fhir_map import FhirMap, RecordType, human_name, render
from profiles.loader import load_profile

# Capitalised words that start questions rather than names.
NOT_NAMES = {"what", "when", "where", "which", "who", "how", "does", "do", "did", "is", "are", "can", "could",
             "should", "show", "list", "check", "find", "tell", "look", "give", "medication", "medications",
             "review", "please", "the", "for", "any", "patient", "mr", "mrs", "ms", "dr", "i", "we", "my", "born",
             "dob", "january", "february", "march", "april", "may", "june", "july", "august", "september",
             "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
             "oct", "nov", "dec"}


def name_candidates(query: str) -> list[tuple[str, str]]:
    """(given, family) pairs for runs of two or more capitalised words in the question."""
    out, run = [], []
    for w in re.finditer(r"\b[\w'-]+\b", query):
        token = re.sub(r"'s$", "", w.group(0))
        if token[:1].isupper() and token.lower() not in NOT_NAMES:
            run.append(token)
        else:
            if len(run) >= 2:
                out.append((run[0], run[-1]))
            run = []
    if len(run) >= 2:
        out.append((run[0], run[-1]))
    return list(dict.fromkeys(out))


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}


def birthdates_in(query: str) -> list[str]:
    """Dates written in the question, as YYYY-MM-DD: 1987-09-12, 9/12/1987 (U.S. order),
    September 12, 1987, 12 Sep 1987."""
    out = []
    for y, mo, d in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", query):
        out.append((y, mo, d))
    for mo, d, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", query):
        out.append((y, mo, d))
    for mon, d, y in re.findall(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})\b", query):
        if mon[:3].lower() in MONTHS:
            out.append((y, MONTHS[mon[:3].lower()], d))
    for d, mon, y in re.findall(r"\b(\d{1,2}) ([A-Za-z]{3,9})\.?,? (\d{4})\b", query):
        if mon[:3].lower() in MONTHS:
            out.append((y, MONTHS[mon[:3].lower()], d))
    dates = []
    for y, mo, d in out:
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            dates.append(f"{int(y):04d}-{int(mo):02d}-{int(d):02d}")
    return list(dict.fromkeys(dates))


NEED_BIRTHDATE = ("The record system finds a patient by full name and date of birth (two identifiers). "
                  "Please ask again with both, for example: Camila Lopez, born 1987-09-12.")


def find_patient(client: FhirClient, m: FhirMap, query: str) -> tuple[dict | None, str | None]:
    """(the patient resource, None) or (None, why not: 'none', 'more than one' or 'birthdate')."""
    found = {}
    dates = birthdates_in(query) if m.needs_birthdate else [None]
    if not dates:
        return None, "birthdate"
    for given, family in name_candidates(query):
        for dob in dates:
            params = {m.name_search["given"]: given, m.name_search["family"]: family}
            if dob:
                params[m.name_search["birthdate"]] = dob
            for res in client.search(m.resource, params, 5):
                found[res.get("id")] = res
    if len(found) == 1:
        return next(iter(found.values())), None
    return None, ("more than one" if found else "none")


def _record(rid, title, text, classes) -> dict:
    return {"id": rid, "title": title, "text": text, "data_classes": classes}


def records_of_type(client: FhirClient, r: RecordType, pid: str, name: str, allowed: set[str]) -> list[dict]:
    detail = r.more_detail if r.more_detail and r.more_detail.data_class in allowed else None
    text = detail.text if detail else r.text
    classes = [r.data_class] + ([detail.data_class] if detail else [])
    found = client.search(r.resource, {r.link: pid, **dict(r.search), "_count": r.limit}, r.limit)
    extra = {"subject": name}
    return [_record(r.source_id.make(res.get("id", "")), render(r.title, res, extra), render(text, res, extra),
                    classes) for res in found]


def search(client: FhirClient | None, m: FhirMap | None, query: str, allowed_classes: list[str]) -> dict:
    allowed = set(allowed_classes)
    if m is None or client is None:
        return {"results": [], "note": "no FHIR records source for this profile"}
    if not allowed:
        return {"results": [], "note": f"this role may not see {m.noun} records"}
    try:
        patient, why = find_patient(client, m, query)
        if patient is None:
            note = {"more than one": f"more than one {m.noun} matches; use the full name",
                    "birthdate": NEED_BIRTHDATE}.get(why, f"no {m.noun} found by that name"
                                                     + (" and date of birth" if m.needs_birthdate else ""))
            return {"results": [], "note": note}
        name, pid = human_name(patient), patient["id"]
        out = []
        if m.subject_class in allowed:
            extra = {"name": name}
            out.append(_record(m.subject_id.make(pid), render(m.subject_title, patient, extra),
                               render(m.subject_text, patient, extra), [m.subject_class]))
        for r in m.records:
            if r.data_class in allowed:                 # not allowed: never requested from the server
                out += records_of_type(client, r, pid, name, allowed)
    except FhirError as e:
        return {"results": [], "note": f"the record system did not answer: {e}"}
    extra = {"status": m.status} if m.status else {}
    return {"results": [{**r, **extra} for r in out[:m.max_results]]}


if __name__ == "__main__":
    PROFILE = load_profile()
    MAP = PROFILE.fhir_map
    CLIENT = None
    if MAP is None:
        print(f"FHIR records for profile {PROFILE.name}: none (no fhir.yaml in the profile)")
    else:
        try:
            CLIENT = client_from_settings(MAP.server)
            meta = CLIENT.capability()
            print(f"FHIR records for profile {PROFILE.name}: {MAP.server['base_url']} "
                  f"(FHIR {meta.get('fhirVersion', '?')}, {(meta.get('software') or {}).get('name', 'server')})")
        except FhirError as e:
            print(f"FHIR records for profile {PROFILE.name}: not reachable yet ({e}); will retry on each request")
    mcp = FastMCP("fhir_records", host="127.0.0.1", port=8104)

    @mcp.tool()
    def search_records(query: str, allowed_classes: list[str]) -> dict:
        """Find the patient named in the question and return the records the caller's role may see
        (allowed_classes), from the FHIR server, with source IDs."""
        global CLIENT
        if CLIENT is None and MAP is not None:
            try:
                CLIENT = client_from_settings(MAP.server)
            except FhirError as e:
                return {"results": [], "note": f"the record system is not set up: {e}"}
        return search(CLIENT, MAP, query, allowed_classes)

    mcp.run(transport="streamable-http")
