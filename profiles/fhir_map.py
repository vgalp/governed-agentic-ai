"""How FHIR resources become records (ADR 013): the FHIR counterpart of records.yaml (ADR 011).

The ideas are the same: a subject (the Patient), record types, a data class for each, texts built
from templates, citable source IDs. What changes is where values come from: a record type names a
FHIR resource type and search parameters instead of a table, and its texts use simple FHIR paths
instead of column names. Another FHIR server (Oracle Health, Meditech, a local HAPI server) is another
mapping, not code.

Paths: `name[0].family`, `period.start`, `medicationCodeableConcept.text`. A list without an index
gives its first item. Alternatives, first one with a value: `{medicationCodeableConcept.text,
medicationReference.display}`. Filters: `{period.start|date}`. In the subject's own texts, `{name}`
is the person's name; in record texts, `{subject}` is the subject's name.
"""

import re
from dataclasses import dataclass

PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
PATH = re.compile(r"^[A-Za-z][A-Za-z0-9]*(\[\d+\])?(\.[A-Za-z][A-Za-z0-9]*(\[\d+\])?)*$")
FILTERS = {"date", "datetime"}
RESOURCE = re.compile(r"^[A-Z][A-Za-z]+$")
AUTHS = {"none", "smart_backend"}


class FhirMappingError(Exception):
    pass


@dataclass(frozen=True)
class Text:
    text: str
    optional: tuple[tuple[str, str], ...] = ()    # (path, text added when the path has a value)


@dataclass(frozen=True)
class SourceId:
    prefix: str
    keep_last: int | None

    def make(self, rid: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9]", "", rid or "")
        return self.prefix + (clean[-self.keep_last:] if self.keep_last else clean)


@dataclass(frozen=True)
class Detail:
    data_class: str
    text: Text


@dataclass(frozen=True)
class RecordType:
    name: str
    resource: str
    link: str                          # search parameter naming the patient, e.g. patient
    search: tuple[tuple[str, str], ...]
    data_class: str
    title: Text
    text: Text
    source_id: SourceId
    limit: int
    more_detail: Detail | None


@dataclass(frozen=True)
class FhirMap:
    server: dict                       # base_url, auth, token_url, *_env, key_id, scope
    noun: str
    resource: str                      # the subject's resource type (Patient)
    name_search: dict                  # {"given": param, "family": param}
    subject_class: str
    subject_title: Text
    subject_text: Text
    subject_id: SourceId
    records: tuple[RecordType, ...]
    max_results: int
    status: str | None

    def data_classes(self) -> set[str]:
        out = {self.subject_class}
        for r in self.records:
            out.add(r.data_class)
            if r.more_detail:
                out.add(r.more_detail.data_class)
        return out

    @property
    def needs_birthdate(self) -> bool:
        """The patient is found by name and date of birth (two identifiers)."""
        return "birthdate" in self.name_search

    def record(self, name: str) -> RecordType | None:
        return next((r for r in self.records if r.name == name), None)


# --- reading values ------------------------------------------------------------------------------

def get_path(resource: dict, path: str):
    value = resource
    for part in path.split("."):
        m = re.match(r"^([A-Za-z0-9]+)(?:\[(\d+)\])?$", part)
        key, idx = m.group(1), m.group(2)
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, dict):
            return None
        value = value.get(key)
        if idx is not None:
            value = value[int(idx)] if isinstance(value, list) and len(value) > int(idx) else None
    if isinstance(value, list):
        value = value[0] if value else None
    return value


def human_name(resource: dict) -> str:
    names = resource.get("name") or []
    n = next((x for x in names if x.get("use") in (None, "official", "usual")), names[0] if names else {})
    if n.get("text"):
        return n["text"]
    return " ".join([*(n.get("given") or []), n.get("family") or ""]).strip()


def _fmt(value, filt: str | None) -> str:
    s = str(value)
    if filt == "date":
        return s[:10]
    if filt == "datetime":
        return s[:16].replace("T", " at ")
    return s


def value_of(resource: dict, expr: str, extra: dict):
    paths, _, filt = expr.partition("|")
    for p in paths.split(","):
        p = p.strip()
        v = extra[p] if p in extra else get_path(resource, p)
        if v not in (None, "", [], {}):
            return _fmt(v, filt or None)
    return None


def render(t: Text, resource: dict, extra: dict) -> str:
    out = PLACEHOLDER.sub(lambda m: value_of(resource, m.group(1), extra) or "unknown", t.text)
    for path, text in t.optional:
        if value_of(resource, path, extra):
            out += PLACEHOLDER.sub(lambda m: value_of(resource, m.group(1), extra) or "", text)
    return out


# --- parsing the mapping -------------------------------------------------------------------------

def _req(d: dict, key: str, where: str):
    if not isinstance(d, dict) or key not in d or d[key] in (None, ""):
        raise FhirMappingError(f"{where}: missing '{key}'")
    return d[key]


def _check(text: str, where: str, specials: set[str]) -> None:
    for expr in PLACEHOLDER.findall(text):
        paths, _, filt = expr.partition("|")
        if filt and filt not in FILTERS:
            raise FhirMappingError(f"{where}: unknown filter '{filt}' (use {sorted(FILTERS)})")
        for p in paths.split(","):
            p = p.strip()
            if p not in specials and not PATH.match(p):
                raise FhirMappingError(f"{where}: '{p}' is not a simple FHIR path")


def _text(raw: dict, where: str, specials: set[str]) -> tuple[Text, Text]:
    title = str(_req(raw, "title", where))
    body = str(_req(raw, "text", where))
    optional = tuple((str(k), str(v)) for k, v in (raw.get("optional") or {}).items())
    for t in [title, body, *(v for _, v in optional)]:
        _check(t, where, specials)
    for k, _ in optional:
        _check("{" + k + "}", where, specials)
    return Text(title), Text(body, optional)


def _source_id(raw, where: str) -> SourceId:
    raw = raw or {}
    prefix = str(_req(raw, "prefix", where + ": source_id"))
    keep = raw.get("keep_last", 8)
    if keep is not None and (not isinstance(keep, int) or keep < 4):
        raise FhirMappingError(f"{where}: source_id keep_last must be a number of at least 4")
    return SourceId(prefix, keep)


def parse(raw: dict, where: str, known_classes: set[str]) -> FhirMap:
    if not isinstance(raw, dict):
        raise FhirMappingError(f"{where}: must be a mapping")
    server = _req(raw, "server", where)
    _req(server, "base_url", f"{where}: server")
    if not re.match(r"^https?://\S+$", server["base_url"]):
        raise FhirMappingError(f"{where}: server base_url must be an http(s) URL")
    auth = server.get("auth", "none")
    if auth not in AUTHS:
        raise FhirMappingError(f"{where}: server auth must be one of {sorted(AUTHS)}")
    if auth == "smart_backend":
        for k in ("token_url", "client_id_env", "private_key_env", "key_id"):
            _req(server, k, f"{where}: server (smart_backend)")
        for k in ("client_id", "private_key"):
            if k in server:
                raise FhirMappingError(f"{where}: server: put {k} in .env and name the variable "
                                       f"({k}_env), never the value in this file")

    s = _req(raw, "subject", where)
    w = f"{where}: subject"
    resource = str(_req(s, "resource", w))
    if not RESOURCE.match(resource):
        raise FhirMappingError(f"{w}: resource must be a FHIR resource type, e.g. Patient")
    ns = _req(s, "name_search", w)
    for k in ("given", "family"):
        _req(ns, k, f"{w}: name_search")
    unknown_keys = set(ns) - {"given", "family", "birthdate"}
    if unknown_keys:
        raise FhirMappingError(f"{w}: name_search takes given, family and birthdate, not {sorted(unknown_keys)}")
    rec = _req(s, "record", w)
    s_title, s_text = _text(rec, f"{w}: record", {"name"})
    s_class = str(_req(rec, "data_class", f"{w}: record"))

    records = []
    for name, r in (raw.get("records") or {}).items():
        rw = f"{where}: records.{name}"
        res = str(_req(r, "resource", rw))
        if not RESOURCE.match(res):
            raise FhirMappingError(f"{rw}: resource must be a FHIR resource type")
        title, text = _text(r, rw, {"subject"})
        detail = None
        if r.get("more_detail"):
            md = r["more_detail"]
            _, dtext = _text({"title": "x", **md}, f"{rw}: more_detail", {"subject"})
            detail = Detail(str(_req(md, "data_class", f"{rw}: more_detail")), dtext)
        limit = r.get("limit", 10)
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise FhirMappingError(f"{rw}: limit must be a number from 1 to 50")
        records.append(RecordType(name, res, str(_req(r, "link", rw)),
                                  tuple((str(k), str(v)) for k, v in (r.get("search") or {}).items()),
                                  str(_req(r, "data_class", rw)), title, text, _source_id(r.get("source_id"), rw),
                                  limit, detail))
    m = FhirMap(server, str(s.get("noun", "patient")), resource, ns, s_class, s_title, s_text,
                _source_id(rec.get("source_id"), f"{w}: record"), tuple(records),
                int(raw.get("max_results", 20)), raw.get("status"))
    unknown = sorted(m.data_classes() - known_classes)
    if unknown:
        raise FhirMappingError(f"{where}: data class(es) not in roles.yaml: {', '.join(unknown)}")
    return m
