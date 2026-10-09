"""What an approved action changes in the records database (the `change:` block of an
action in actions.yaml).

The change server (mcp_servers/changes/server.py) is general-purpose: it makes exactly
the change described here, and nothing else. The caller (the executor agent) only sends
the proposal's field values and the approval ID; which table, which record, which
columns and which precondition all come from the profile, on the server side.

  change:
    record: appointments            # a record type in records.yaml: its table, ID column and ID prefix
    id_field: appointment_id        # the proposal field that holds the record's cited ID
    set_from: {starts_at: new_start}   # column <- proposal field
    set: {}                         # column <- fixed value, e.g. {status: issued}
    only_if: {status: scheduled}    # made only if the record still has these values
  done_text: "{id} moved from {before_starts_at|datetime} to {after_starts_at|datetime}"

Like records.yaml, this holds no SQL: only names, checked here and against the database
when the change server starts. The record's ID column and the column linking it to its
subject can never be changed, and a record type whose cited IDs are shortened (keep_last)
can't be changed, because its cited ID can't be traced back to exactly one row.
"""

from dataclasses import dataclass

from profiles.records_map import (FILTERS, IDENT_RE, MappingError, RecordsMap, Template,
                                  placeholders)


@dataclass(frozen=True)
class Change:
    record: str                                 # record type name in records.yaml
    table: str
    id_column: str
    prefix: str                                 # cited ID = prefix + stored ID
    id_field: str                               # proposal field holding the cited ID
    set_values: tuple[tuple[str, object], ...]  # (column, fixed value)
    set_from: tuple[tuple[str, str], ...]       # (column, proposal field)
    only_if: tuple[tuple[str, object], ...]     # (column, value the record must still have)
    done_text: Template | None                  # how the result reads

    def columns(self) -> list[str]:
        """The columns this change writes, in a stable order."""
        return [c for c, _ in self.set_values] + [c for c, _ in self.set_from]

    def stored_id(self, cited: str) -> str | None:
        """The stored ID for a cited one (apt-0001 -> apt-0001, pay-5004 -> 5004)."""
        cited = str(cited)
        if not cited.startswith(self.prefix):
            return None
        return cited[len(self.prefix):] or None


def _ident(value, where: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.match(value):
        raise MappingError(f"{where}: '{value}' must be a column name (letters, digits and _ only)")
    return value


def _scalar_map(raw, where: str, what: str) -> tuple[tuple[str, object], ...]:
    if raw in (None, {}):
        return ()
    if not isinstance(raw, dict):
        raise MappingError(f"{where}: {what} maps a column to a value")
    for col, v in raw.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise MappingError(f"{where}: {what} '{col}' must be a single value or null")
    return tuple((_ident(c, f"{where}: {what}"), v) for c, v in raw.items())


def parse(raw: dict, done_text, where: str, fields: dict[str, dict], records_map: RecordsMap | None) -> Change:
    """Parse and check an action's change block. fields: the action's checked fields."""
    if records_map is None:
        raise MappingError(f"{where}: a change needs the profile's records.yaml (records_map)")
    if not isinstance(raw, dict):
        raise MappingError(f"{where}: change must be a mapping")
    record = raw.get("record")
    rt = next((r for r in records_map.records if r.name == record), None)
    if rt is None:
        names = ", ".join(r.name for r in records_map.records)
        raise MappingError(f"{where}: record '{record}' is not a record type in records.yaml ({names})")
    if rt.source_id.keep_last:
        raise MappingError(f"{where}: record '{record}' shortens its cited IDs (keep_last), so a cited "
                           "ID can't be traced back to one row; it can't be changed")
    id_field = raw.get("id_field")
    if fields.get(id_field, {}).get("type") != "source":
        raise MappingError(f"{where}: id_field '{id_field}' must be a field of type source")

    set_values = _scalar_map(raw.get("set"), where, "set")
    set_from_raw = raw.get("set_from") or {}
    if not isinstance(set_from_raw, dict):
        raise MappingError(f"{where}: set_from maps a column to a proposal field")
    set_from = []
    for col, f in set_from_raw.items():
        if f not in fields or f == id_field:
            raise MappingError(f"{where}: set_from '{col}' must name a proposal field (not the ID field)")
        if fields[f].get("type") == "source":
            raise MappingError(f"{where}: set_from '{col}' can't take a source ID field")
        set_from.append((_ident(col, f"{where}: set_from"), f))
    only_if = _scalar_map(raw.get("only_if"), where, "only_if")

    written = [c for c, _ in set_values] + [c for c, _ in set_from]
    if not written:
        raise MappingError(f"{where}: the change writes nothing (give set or set_from)")
    if len(set(written)) != len(written):
        raise MappingError(f"{where}: a column is written twice")
    for c in written:
        if c in (rt.source_id.column, rt.link):
            raise MappingError(f"{where}: '{c}' identifies the record or links it to its subject; "
                               "it can never be changed")

    done = None
    if done_text:
        done = Template(str(done_text), ())
        allowed = {"id"} | {f"{p}_{c}" for c in written for p in ("before", "after")}
        try:
            found = placeholders(done.text)
        except (ValueError, MappingError) as e:
            raise MappingError(f"{where}: done_text: {e}") from e
        for name, filt in found:
            if name not in allowed:
                raise MappingError(f"{where}: done_text: '{{{name}}}' is not one of {sorted(allowed)}")
            if filt and filt not in FILTERS:
                raise MappingError(f"{where}: done_text: unknown filter '{filt}'")
    return Change(record, rt.table, rt.source_id.column, rt.source_id.prefix, id_field,
                  set_values, tuple(set_from), only_if, done)