"""MCP server: the patient's records from a FHIR server (Epic's sandbox, or any FHIR R4 server), ADR 013.

The FHIR counterpart of mcp_servers/records: the same tool (`search_records`), the same records (id,
title, text, data classes) and the same rules, so the gateway, the roles and the answer checks work
unchanged. What it reads, and how values become text, comes from the profile's fhir.yaml.

- Read-only: the FHIR client only sends GET requests (fhir/client.py).
- Need-to-know: a resource type is requested only when the caller's role may see its data class, so
  data a role may not see is never fetched from the record system.
- The patient is found by name, from the question: given and family name, searched on the server.
  Only exact names count (a FHIR name search also returns longer names that start the same way).
  More than one match, or none, returns nothing. When the sign-in is for one patient (smart_user,
  ADR 032), that patient is read and must match the name and date of birth; nothing is searched.
"""

import re

from mcp.server.fastmcp import FastMCP

from fhir.capabilities import Capabilities, check_map
from fhir.client import FhirClient, FhirError, client_from_settings
from fhir.smart_launch import launch_client
from fhir.transport import request_budget
from mcp_servers.common.serve import serve
from privacy.sensitivity import resource_tagger
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


OTHER_PATIENT = ("This conversation is for the patient whose chart you opened. To look at another patient, "
                 "open their chart and launch the assistant from there.")
NEED_BIRTHDATE = ("The record system finds a patient by full name and date of birth (two identifiers). "
                  "Please ask again with both, for example: Camila Lopez, born 1987-09-12.")


MRN_RE = re.compile(r"\b(?:MRN|medical record (?:number|no\.?)|record (?:number|no\.?))\s*[:#]?\s*([A-Za-z0-9-]{3,20})\b",
                    re.IGNORECASE)


def find_patient(client: FhirClient, m: FhirMap, query: str) -> tuple[dict | None, str | None]:
    """(the patient resource, None) or (None, why not: 'none', 'more than one', 'birthdate' or
    'other patient')."""
    auth = getattr(client, "auth", None)
    if getattr(auth, "from_chart", False):
        # Launched from the patient's chart (ADR 035): that patient, no name needed. A birth date in
        # the question that is not this patient's means another patient: refused, never searched.
        res = client.read(m.resource, auth.patient)
        named = birthdates_in(query)
        return (None, "other patient") if named and res.get("birthDate") not in named else (res, None)
    found = {}
    dates = birthdates_in(query) if m.needs_birthdate else [None]
    if not dates:
        return None, "birthdate"
    pinned = getattr(auth, "patient", None)
    if pinned:                     # a sign-in for one patient (smart_user): read it, never search
        res = client.read(m.resource, pinned)
        return (res, None) if _is_named(res, query, dates) else (None, "none")
    mrn = MRN_RE.search(query) if m.identifier else None
    if mrn:                        # a record number: exact, so no name search; the birth date still has to match
        system = m.identifier.get("system")
        value = f"{system}|{mrn.group(1)}" if system else mrn.group(1)
        hits = [res for res in client.search(m.resource, {m.identifier["param"]: value}, 5)
                if dates == [None] or res.get("birthDate") in dates]
        return (hits[0], None) if len(hits) == 1 else (None, "more than one" if hits else "none")
    for given, family in name_candidates(query):
        for dob in dates:
            params = {m.name_search["given"]: given, m.name_search["family"]: family}
            if dob:
                params[m.name_search["birthdate"]] = dob
            for res in client.search(m.resource, params, 20):
                # FHIR name search matches the start of a name (Wilm finds Wilma): keep exact names only.
                # The server has already matched the date of birth.
                if _is_named(res, f"{given} {family}", [None]):
                    found[res.get("id")] = res
    if len(found) == 1:
        return next(iter(found.values())), None
    return None, ("more than one" if found else "none")


def _is_named(res: dict, query: str, dates: list) -> bool:
    """The question names this patient: a given and family name on the record, and its date of birth
    when the mapping asks for one. The same two identifiers a search would use."""
    def low(x):
        return (x or "").casefold()
    names = res.get("name") or []
    pairs = {(low(g), low(n.get("family"))) for n in names for g in (n.get("given") or [])}
    named = any((low(g), low(f)) in pairs for g, f in name_candidates(query))
    return named and (dates == [None] or res.get("birthDate") in dates)


def _record(rid, title, text, classes, res: dict | None = None, tag=None) -> dict:
    """`tag` (privacy.sensitivity.resource_tagger) adds the sensitive categories the resource's own
    labels and codes show (ADR 038): the gateway cannot see those, only the text."""
    record = {"id": rid, "title": title, "text": text, "data_classes": classes}
    if tag is not None and res is not None:
        record["sensitivity"] = tag(res)
    return record


def records_of_type(client: FhirClient, r: RecordType, pid: str, name: str, allowed: set[str], tag=None) -> list[dict]:
    detail = r.more_detail if r.more_detail and r.more_detail.data_class in allowed else None
    text = detail.text if detail else r.text
    classes = [r.data_class] + ([detail.data_class] if detail else [])
    found = r.arrange(client.search(r.resource, r.params(pid), r.limit))
    extra = {"subject": name}
    return [_record(r.source_id.make(res.get("id", "")), render(r.title, res, extra), render(text, res, extra),
                    classes, res, tag) for res in found]


def search(client: FhirClient | None, m: FhirMap | None, query: str, allowed_classes: list[str], tag=None) -> dict:
    allowed = set(allowed_classes)
    if m is None or client is None:
        return {"results": [], "note": "no FHIR records source for this profile"}
    if not allowed:
        return {"results": [], "note": f"this role may not see {m.noun} records"}
    try:
        patient, why = find_patient(client, m, query)
        if patient is None:
            note = {"more than one": f"more than one {m.noun} matches; use the full name",
                    "other patient": OTHER_PATIENT,
                    "birthdate": NEED_BIRTHDATE}.get(why, f"no {m.noun} found by that name"
                                                     + (" and date of birth" if m.needs_birthdate else ""))
            return {"results": [], "note": note}
        name, pid = human_name(patient), patient["id"]
        out = []
        if m.subject_class in allowed:
            extra = {"name": name}
            out.append(_record(m.subject_id.make(pid), render(m.subject_title, patient, extra),
                               render(m.subject_text, patient, extra), [m.subject_class], patient, tag))
        unread = []
        for r in m.records:
            if r.data_class in allowed:                 # not allowed: never requested from the server
                try:
                    out += records_of_type(client, r, pid, name, allowed, tag)
                except FhirError as e:                  # one resource type the server refuses (not granted
                    unread.append(f"{r.name} ({e})")    # to the app yet) must not lose the others
    except FhirError as e:
        return {"results": [], "note": f"the record system did not answer: {e}"}
    extra = {"status": m.status} if m.status else {}
    result = {"results": [{**r, **extra} for r in out[:m.max_results]]}
    if unread:
        result["note"] = "the record system did not answer for: " + "; ".join(unread)
    return result


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
            for finding in check_map(Capabilities.parse(meta), MAP):   # what the server may not apply
                print(f"  {finding}")
        except FhirError as e:
            print(f"FHIR records for profile {PROFILE.name}: not reachable yet ({e}); will retry on each request")
    TAG = resource_tagger(PROFILE.sensitivity)   # None when the profile tracks no sensitive categories
    mcp = FastMCP("fhir_records", host="127.0.0.1", port=8104)

    @mcp.tool()
    def search_records(query: str, allowed_classes: list[str], launch_ref: str | None = None) -> dict:
        """Find the patient named in the question and return the records the caller's role may see
        (allowed_classes), from the FHIR server, with source IDs."""
        global CLIENT
        with request_budget(MAP.server if MAP is not None else None):   # all requests together (ADR 036)
            if launch_ref and MAP is not None and MAP.launch:   # set by the gateway, never by a caller
                try:
                    return search(launch_client(MAP, launch_ref, PROFILE.name), MAP, query, allowed_classes, TAG)
                except FhirError as e:
                    return {"results": [], "note": str(e)}
            if CLIENT is None and MAP is not None:
                try:
                    CLIENT = client_from_settings(MAP.server)
                except FhirError as e:
                    return {"results": [], "note": f"the record system is not set up: {e}"}
            return search(CLIENT, MAP, query, allowed_classes, TAG)

    serve(mcp)   # plain HTTP, or mutual TLS (ADR 030)
