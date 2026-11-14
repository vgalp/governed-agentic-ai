"""Find a patient and read their records, from either records source (ADR 027).

The medication review, the chart brief and the medication list each used to carry their own copy
of: open the source, find the patient named in the question (full name, or name and date of
birth on FHIR), say why not, read a record type's rows. This is the one copy.

    src = Source.open(profile, kind)          # "db" (records.yaml) or "fhir" (fhir.yaml)
    found = src.find(query)                   # Patient, or NotFound with the reply to give
    rows  = src.rows(record_type, found.id)   # structured rows of one record type, each with "_id"
    recs  = src.records(record_type, found, allowed)   # cited lines, as the records tools build them

Nothing here decides anything or calls a model. The replies are the same words the tools used.
"""

from dataclasses import dataclass, field
from pathlib import Path

from profiles.loader import Profile

NOT_FOUND = {
    "several": "More than one patient matches; please use the full name.",
    "none": "I couldn't find that patient. Please use the full name as it appears in the record.",
}
NEED_BIRTHDATE = (
    "The record system finds a patient by full name and date of birth (two identifiers). "
    "Please ask again with both, for example: Camila Lopez, born 1987-09-12."
)


@dataclass(frozen=True)
class Patient:
    id: str  # the source's own ID (row id or FHIR id)
    name: str
    cited_id: str  # the ID an answer cites (pt-…)
    text: str  # the subject line ("Harold Brinkley, born 1948-03-14, …")
    resource: dict | None = None  # the FHIR Patient resource, on that source


@dataclass(frozen=True)
class NotFound:
    why: str  # none · several · birthdate · unavailable
    reply: str


@dataclass
class Source:
    kind: str  # db or fhir
    profile: Profile
    db_path: Path | None = None
    client: object = None  # fhir.client.FhirClient
    unread: dict = field(default_factory=dict)  # record type -> error, for FHIR types the app may not read yet
    cut: dict = field(default_factory=dict)  # record type -> how many were read, when the record has more
    _fetched: dict = field(default_factory=dict, repr=False)  # (record type, patient id) -> resources, one request each

    # --- opening ----------------------------------------------------------------------------------

    @classmethod
    def open(cls, profile: Profile, kind: str, db_path: Path | None = None, client=None) -> "Source | None":
        """The source, or None when the profile does not have it (the tool then says it is not set up)."""
        if kind == "fhir":
            return cls("fhir", profile, client=client) if profile.fhir_map is not None and client is not None else None
        if profile.records_map is None or db_path is None or not Path(db_path).exists():
            return None
        return cls("db", profile, db_path=Path(db_path))

    @property
    def mapping(self):
        return self.profile.fhir_map if self.kind == "fhir" else self.profile.records_map

    def record_type(self, name: str):
        return next((r for r in self.mapping.records if r.name == name), None)

    # --- the patient ------------------------------------------------------------------------------

    def find(self, query: str) -> Patient | NotFound:
        if self.kind == "fhir":
            return self._find_fhir(query)
        return self._find_db(query)

    def _find_db(self, query: str) -> Patient | NotFound:
        import sqlite3

        from mcp_servers.records.server import _select, find_subjects
        from profiles.records_map import render_template

        m = self.mapping
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            found = find_subjects(db, m, query)
            if len(found) != 1:
                return NotFound("several" if found else "none", NOT_FOUND["several" if found else "none"])
            sid, name = found[0]
            s = m.record
            row = _select(db, m.table, {s.source_id.column} | s.template.columns() | s.title.columns(), [(m.id, sid)])[0]
            return Patient(sid, name, s.source_id.make(row[s.source_id.column]), render_template(s.template, row))
        finally:
            db.close()

    def _find_fhir(self, query: str) -> Patient | NotFound:
        from fhir.client import FhirError
        from mcp_servers.fhir_records.server import find_patient
        from profiles.fhir_map import human_name, render

        m = self.mapping
        try:
            patient, why = find_patient(self.client, m, query)
        except FhirError as e:
            return NotFound("unavailable", f"The record system did not answer: {e}")
        if patient is None:
            if why == "birthdate":
                return NotFound("birthdate", NEED_BIRTHDATE)
            if why == "other patient":
                from mcp_servers.fhir_records.server import OTHER_PATIENT
                return NotFound("other", OTHER_PATIENT)
            key = "several" if why == "more than one" else "none"
            return NotFound(key, NOT_FOUND[key])
        name = human_name(patient)
        return Patient(
            patient["id"], name, m.subject_id.make(patient["id"]), render(m.subject_text, patient, {"name": name}), patient
        )

    @property
    def subject_class(self) -> str:
        return self.mapping.subject_class if self.kind == "fhir" else self.mapping.record.data_class

    # --- records ----------------------------------------------------------------------------------

    def records(self, r, patient: Patient, allowed: set[str]) -> list[dict]:
        """The cited lines of one record type, exactly as the records tools build them."""
        if self.kind == "fhir":
            return self._fhir_records(r, patient, allowed)
        import sqlite3

        from mcp_servers.records.server import _records_of_type

        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return _records_of_type(db, r, patient.id, patient.name, allowed)
        finally:
            db.close()

    def resources(self, r, patient: Patient) -> list[dict]:
        """FHIR: the resources of one record type for the patient (empty, and noted in `unread`,
        when the server refuses the type: not granted to the app yet)."""
        from fhir.client import FhirError

        key = (r.name, patient.id)
        if key not in self._fetched:          # the lines and the checks read the same resources: ask once
            try:
                params = r.params(patient.id)
                if hasattr(self.client, "search_all"):
                    found, more = self.client.search_all(r.resource, params, r.limit)
                else:                              # a client that cannot tell (a test double)
                    found, more = self.client.search(r.resource, params, r.limit), False
                self._fetched[key] = r.arrange(found)  # newest first when the mapping asks: not the server's order
                if more:
                    self.cut[r.name] = len(found)
            except FhirError as e:
                self.unread[r.name] = str(e)
                self._fetched[key] = []
        return self._fetched[key]

    def prefetch(self, types, patient: Patient) -> None:
        """FHIR: read several record types at once (each is one or more GETs; a slow server answers
        them side by side instead of one after another). Read-only, so the order does not matter."""
        if self.kind != "fhir":
            return
        from concurrent.futures import ThreadPoolExecutor

        from fhir.client import FhirError
        from fhir.resilience import run_in_context

        if getattr(self.client, "auth", None) is not None:
            try:
                self.client.auth.token()      # sign in once, before the parallel reads
            except FhirError:
                pass                          # each read below reports it, under its own record type
        types = [r for r in types if (r.name, patient.id) not in self._fetched]
        with ThreadPoolExecutor(max_workers=max(1, min(6, len(types)))) as pool:
            # each read runs in a copy of this context: the tool call's time budget applies there too
            futures = [pool.submit(run_in_context(self.resources, r, patient)) for r in types]
            for f in futures:
                f.result()

    def _fhir_records(self, r, patient: Patient, allowed: set[str]) -> list[dict]:
        from privacy.sensitivity import resource_tagger
        from profiles.fhir_map import render

        detail = r.more_detail if r.more_detail and r.more_detail.data_class in allowed else None
        text = detail.text if detail else r.text
        classes = [r.data_class] + ([detail.data_class] if detail else [])
        tag = resource_tagger(self.profile.sensitivity)   # the resource's own labels and codes (ADR 038)
        return [
            {
                "id": r.source_id.make(res.get("id", "")),
                "title": render(r.title, res, {"subject": patient.name}),
                "text": render(text, res, {"subject": patient.name}),
                "data_classes": classes,
                **({"sensitivity": tag(res)} if tag else {}),
            }
            for res in self.resources(r, patient)
        ]

    def rows(self, r, patient: Patient, fields: dict[str, str]) -> list[dict]:
        """Structured rows of one record type: {field: value, "_id": cited id}. `fields` maps a field
        name to a column (db) or a FHIR path (fhir; a,b takes the first with a value)."""
        if self.kind == "fhir":
            from profiles.fhir_map import value_of

            out = []
            for res in self.resources(r, patient):
                out.append(
                    {
                        **{f: value_of(res, p, {}) for f, p in fields.items()},
                        "_id": r.source_id.make(res.get("id", "")),
                        "_resource": res,
                    }
                )
            return out
        import sqlite3

        from mcp_servers.records.server import _select

        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            cols = set(fields.values()) | {r.source_id.column}
            rows = _select(db, r.table, cols, [(r.link, patient.id), *r.only_where], r.order_by, r.limit)
            return [
                {**{f: row.get(c) for f, c in fields.items()}, "_id": r.source_id.make(row[r.source_id.column])} for row in rows
            ]
        finally:
            db.close()
