"""The records mapping (records.yaml): how a profile's own database becomes records.

The records server is general-purpose. A profile's records.yaml says which table holds
the people a question can be about (the subject: a patient, an applicant, a client),
which tables hold their records, which data class each record belongs to, and how each
record reads. A new organization connects its data by writing this file, not code.

The mapping holds no SQL. It names tables and columns, and the records server builds
every query itself, read-only, with values passed as parameters. Names are checked
here (letters, digits and _ only) and against the real database when it is opened.

A record type may have more_detail: extra columns (e.g. what a visit was for) shown
only to roles that may also see another data class. Columns are read only when the
role may see them, so data a role may not see is never read from the database.
"""

import re
import string
from dataclasses import dataclass

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FILTERS = {"datetime", "date", "money"}
SUBJECT_FIELD = "subject"       # in a record's text: the subject's name


class MappingError(Exception):
    """records.yaml is missing something or contradicts itself."""


@dataclass(frozen=True)
class Template:
    """Text with {column} or {column|filter} placeholders, and optional pieces that are
    added at the end only when their column has a value."""
    text: str
    optional: tuple[tuple[str, str], ...]    # (column, text added when the column has a value)

    def columns(self) -> set[str]:
        cols = {c for c, _ in placeholders(self.text)}
        for col, text in self.optional:
            cols |= {col} | {c for c, _ in placeholders(text)}
        return cols - {SUBJECT_FIELD}


@dataclass(frozen=True)
class SourceId:
    column: str
    prefix: str             # added in front, e.g. med-
    keep_last: int | None   # keep only the last N characters of the stored ID

    def make(self, value) -> str:
        v = str(value)
        return self.prefix + (v[-self.keep_last:] if self.keep_last else v)


@dataclass(frozen=True)
class Detail:
    """Extra detail shown only to roles that may also see data_class."""
    data_class: str
    template: Template


@dataclass(frozen=True)
class RecordType:
    name: str
    table: str
    link: str                                   # column pointing to the subject's id
    source_id: SourceId
    data_class: str
    title: Template
    template: Template
    only_where: tuple[tuple[str, object], ...]  # (column, value); None means IS NULL
    order_by: tuple[tuple[str, bool], ...]      # (column, descending)
    limit: int | None
    more_detail: Detail | None


@dataclass(frozen=True)
class SubjectRecord:
    source_id: SourceId
    data_class: str
    title: Template
    template: Template


@dataclass(frozen=True)
class RecordsMap:
    noun: str                 # what a subject is called, e.g. patient
    table: str
    id: str
    name: str                 # matched against the question
    record: SubjectRecord     # the "about this person" record
    records: tuple[RecordType, ...]
    max_results: int
    status: str | None        # added to every record, e.g. "synthetic record"

    def data_classes(self) -> set[str]:
        out = {self.record.data_class}
        for r in self.records:
            out.add(r.data_class)
            if r.more_detail:
                out.add(r.more_detail.data_class)
        return out

    def tables(self) -> dict[str, set[str]]:
        """Every table and column the mapping names, to check against the database."""
        need = {self.table: {self.id, self.name, self.record.source_id.column}
                | self.record.template.columns() | self.record.title.columns()}
        for r in self.records:
            cols = need.setdefault(r.table, set())
            cols |= {r.link, r.source_id.column} | r.template.columns() | r.title.columns()
            cols |= {c for c, _ in r.only_where} | {c for c, _ in r.order_by}
            if r.more_detail:
                cols |= r.more_detail.template.columns()
        return need


# --- parsing ------------------------------------------------------------------

def placeholders(text: str) -> list[tuple[str, str | None]]:
    """The {column} and {column|filter} placeholders in a text."""
    out = []
    for _, field, spec, conv in string.Formatter().parse(text):
        if field is None:
            continue
        if spec or conv:
            raise MappingError(f"'{{{field}...}}': use {{column}} or {{column|filter}}, "
                               "not Python format codes")
        col, _, filt = field.partition("|")
        out.append((col, filt or None))
    return out


def render(text: str, row: dict, subject: str | None = None) -> str:
    """Fill a text from a row. Missing values read 'not recorded'."""
    out = []
    for literal, field, _, _ in string.Formatter().parse(text):
        out.append(literal)
        if field is None:
            continue
        col, _, filt = field.partition("|")
        value = subject if col == SUBJECT_FIELD else row.get(col)
        out.append(_format(value, filt or None))
    return "".join(out)


def render_template(t: Template, row: dict, subject: str | None = None) -> str:
    text = render(t.text, row, subject)
    for col, extra in t.optional:
        if row.get(col) not in (None, ""):
            text += render(extra, row, subject)
    return text


def _format(value, filt: str | None) -> str:
    if filt == "money":
        return f"{float(value or 0):.2f}"
    if value is None or value == "":
        return "not recorded"
    if filt == "datetime":
        return str(value).replace("T", " at ")
    if filt == "date":
        return str(value)[:10]
    return str(value)


def _ident(value, where: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.match(value):
        raise MappingError(f"{where}: '{value}' must be a table or column name "
                           "(letters, digits and _ only)")
    return value


def _require(d: dict, key: str, where: str):
    if not isinstance(d, dict) or d.get(key) in (None, ""):
        raise MappingError(f"{where}: missing '{key}'")
    return d[key]


def _template(raw: dict, where: str, allow_subject: bool) -> Template:
    text = str(_require(raw, "text", where))
    optional = raw.get("optional") or {}
    if not isinstance(optional, dict):
        raise MappingError(f"{where}: 'optional' maps a column to the text added when it has a value")
    t = Template(text, tuple((_ident(c, f"{where}: optional"), str(v)) for c, v in optional.items()))
    _check_text(t.text, where, allow_subject)
    for _, v in t.optional:
        _check_text(v, where, allow_subject)
    return t


def _check_text(text: str, where: str, allow_subject: bool) -> None:
    try:
        found = placeholders(text)
    except ValueError as e:
        raise MappingError(f"{where}: text '{text}' is not valid: {e}") from e
    except MappingError as e:
        raise MappingError(f"{where}: {e}") from e
    for col, filt in found:
        if col == SUBJECT_FIELD and not allow_subject:
            raise MappingError(f"{where}: {{subject}} is only for record texts; use the name column here")
        if col != SUBJECT_FIELD:
            _ident(col, f"{where}: placeholder")
        if filt and filt not in FILTERS:
            raise MappingError(f"{where}: unknown filter '{filt}' (use one of {sorted(FILTERS)})")


def _source_id(raw, default_column: str, where: str) -> SourceId:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise MappingError(f"{where}: source_id must be a mapping (column, prefix, keep_last)")
    keep = raw.get("keep_last")
    if keep is not None and (not isinstance(keep, int) or keep <= 0):
        raise MappingError(f"{where}: keep_last must be a positive whole number")
    prefix = str(raw.get("prefix", ""))
    if prefix and not re.match(r"^[a-z][a-z0-9]*-$", prefix):
        raise MappingError(f"{where}: prefix '{prefix}' should look like 'med-' (lowercase, ending in -)")
    return SourceId(_ident(raw.get("column", default_column), f"{where}: column"), prefix, keep)


def _order_by(raw, where: str) -> tuple[tuple[str, bool], ...]:
    out = []
    for item in raw or []:
        parts = str(item).split()
        if len(parts) not in (1, 2) or (len(parts) == 2 and parts[1].lower() not in ("asc", "desc")):
            raise MappingError(f"{where}: order_by '{item}' must be 'column' or 'column desc'")
        out.append((_ident(parts[0], f"{where}: order_by"), len(parts) == 2 and parts[1].lower() == "desc"))
    return tuple(out)


def _only_where(raw, where: str) -> tuple[tuple[str, object], ...]:
    if not raw:
        return ()
    if not isinstance(raw, dict):
        raise MappingError(f"{where}: only_where maps a column to a value (or null)")
    for col, v in raw.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise MappingError(f"{where}: only_where '{col}' must be a single value or null")
    return tuple((_ident(c, f"{where}: only_where"), v) for c, v in raw.items())


def parse(raw: dict, where: str, known_classes: set[str]) -> RecordsMap:
    """Parse and check records.yaml. known_classes: the data classes in roles.yaml."""
    s = _require(raw, "subject", where)
    sw = f"{where}: subject"
    table = _ident(_require(s, "table", sw), f"{sw}: table")
    id_col = _ident(s.get("id", "id"), f"{sw}: id")
    rec = _require(s, "record", sw)
    rw = f"{sw}: record"
    subject_record = SubjectRecord(
        _source_id(rec.get("source_id"), id_col, rw),
        str(_require(rec, "data_class", rw)),
        _template({"text": _require(rec, "title", rw)}, f"{rw}: title", allow_subject=False),
        _template(rec, rw, allow_subject=False),
    )

    records, names = [], set()
    for name, r in (raw.get("records") or {}).items():
        w = f"{where}: records: {name}"
        if not isinstance(r, dict):
            raise MappingError(f"{w}: must be a mapping")
        if name in names:
            raise MappingError(f"{w}: listed twice")
        names.add(name)
        detail = None
        if r.get("more_detail"):
            d = r["more_detail"]
            dw = f"{w}: more_detail"
            detail = Detail(str(_require(d, "data_class", dw)), _template(d, dw, allow_subject=True))
        limit = r.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            raise MappingError(f"{w}: limit must be a positive whole number")
        rt_id = _ident(r.get("id", "id"), f"{w}: id")
        records.append(RecordType(
            name=name,
            table=_ident(_require(r, "table", w), f"{w}: table"),
            link=_ident(_require(r, "link", w), f"{w}: link"),
            source_id=_source_id(r.get("source_id"), rt_id, w),
            data_class=str(_require(r, "data_class", w)),
            title=_template({"text": _require(r, "title", w)}, f"{w}: title", allow_subject=True),
            template=_template(r, w, allow_subject=True),
            only_where=_only_where(r.get("only_where"), w),
            order_by=_order_by(r.get("order_by"), w),
            limit=limit,
            more_detail=detail,
        ))

    max_results = raw.get("max_results", 15)
    if not isinstance(max_results, int) or max_results <= 0:
        raise MappingError(f"{where}: max_results must be a positive whole number")
    m = RecordsMap(
        noun=str(s.get("noun", "person")),
        table=table, id=id_col,
        name=_ident(_require(s, "name", sw), f"{sw}: name"),
        record=subject_record, records=tuple(records), max_results=max_results,
        status=str(raw["status"]) if raw.get("status") else None,
    )
    for c in sorted(m.data_classes()):
        if c not in known_classes:
            raise MappingError(f"{where}: data class '{c}' is not in roles.yaml")
    for r in m.records:
        if r.more_detail and r.more_detail.data_class == r.data_class:
            raise MappingError(f"{where}: records: {r.name}: more_detail needs a different data class")
    return m