"""MCP server: a read-only search over the active profile's records database.

General-purpose: what the database holds and how it becomes records comes from the
profile's records.yaml (profiles/records_map.py). For the clinic that is patients,
appointments, medications, visits and claims; another profile maps its own tables.

It finds the person named in the question (the subject) and returns their records,
each with a source ID the answer can cite. It runs locally and is the only tool that
receives real names; the gateway audits the masked question.

Records are built only from the data classes the caller's role may see (decided by OPA
and passed in by the gateway), and only the columns those records use are read: data a
role may not see is never read from the database. Each record lists its classes, so
the gateway can check it again.

Every query is built here from the mapping's table and column names (checked against
the database at startup), with values passed as parameters. The database is opened
read-only. The mapping itself holds no SQL.
"""

import re
import sqlite3
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from profiles.loader import load_profile
from profiles.records_map import RecordsMap, RecordType, render_template

PROFILE = load_profile()

mcp = FastMCP("records", host="127.0.0.1", port=8101)


def _q(name: str) -> str:
    """Quote a table or column name. Names are already checked to be plain identifiers."""
    return f'"{name}"'


def check_schema(db: sqlite3.Connection, m: RecordsMap) -> list[str]:
    """Problems between the mapping and the database: missing tables or columns."""
    problems = []
    for table, cols in sorted(m.tables().items()):
        have = {row[1] for row in db.execute(f"PRAGMA table_info({_q(table)})")}
        if not have:
            problems.append(f"table '{table}' is not in the database")
            continue
        problems += [f"column '{table}.{c}' is not in the database" for c in sorted(cols - have)]
    return problems


def find_subjects(db: sqlite3.Connection, m: RecordsMap, query: str) -> list[tuple[str, str]]:
    """Subjects named in the query: full name first, else a last name only one subject has."""
    q = query.lower()
    rows = db.execute(f"SELECT {_q(m.id)}, {_q(m.name)} FROM {_q(m.table)}").fetchall()
    full = [(sid, name) for sid, name in rows if name and name.lower() in q]
    if full:
        return full
    words = set(re.findall(r"[a-z'-]+", q))
    by_last = [(sid, name) for sid, name in rows if name and name.split()[-1].lower() in words]
    return by_last if len(by_last) == 1 else []


def _record(rid: str, title: str, text: str, classes: list[str]) -> dict:
    return {"id": rid, "title": title, "text": text, "data_classes": classes}


def _select(db: sqlite3.Connection, table: str, cols: set[str], where: list[tuple[str, object]],
            order_by=(), limit: int | None = None) -> list[dict]:
    """Read only the named columns. Values go in as parameters, never into the SQL text."""
    cols = sorted(cols)
    conds, params = [], []
    for col, value in where:
        if value is None:
            conds.append(f"{_q(col)} IS NULL")
        else:
            conds.append(f"{_q(col)} = ?")
            params.append(value)
    sql = f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(table)}"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    if order_by:
        sql += " ORDER BY " + ", ".join(f"{_q(c)}{' DESC' if desc else ''}" for c, desc in order_by)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(zip(cols, row)) for row in db.execute(sql, params)]


def _records_of_type(db: sqlite3.Connection, r: RecordType, sid: str, name: str, allowed: set[str]) -> list[dict]:
    detail = r.more_detail if r.more_detail and r.more_detail.data_class in allowed else None
    template = detail.template if detail else r.template
    classes = [r.data_class] + ([detail.data_class] if detail else [])
    rows = _select(db, r.table, {r.source_id.column} | template.columns() | r.title.columns(),
                   [(r.link, sid), *r.only_where], r.order_by, r.limit)
    return [_record(r.source_id.make(row[r.source_id.column]), render_template(r.title, row, name),
                    render_template(template, row, name), classes) for row in rows]


def subject_records(db: sqlite3.Connection, m: RecordsMap, sid: str, name: str, allowed: set[str]) -> list[dict]:
    """Build the subject's records from the allowed data classes only, in mapping order."""
    out = []
    s = m.record
    if s.data_class in allowed:
        cols = {s.source_id.column} | s.template.columns() | s.title.columns()
        row = _select(db, m.table, cols, [(m.id, sid)])[0]
        out.append(_record(s.source_id.make(row[s.source_id.column]), render_template(s.title, row),
                           render_template(s.template, row), [s.data_class]))
    for r in m.records:
        if r.data_class in allowed:
            out += _records_of_type(db, r, sid, name, allowed)
    return out


def search(db_path: Path | None, query: str, allowed_classes: list[str], m: RecordsMap | None) -> dict:
    allowed = set(allowed_classes)
    if m is None or db_path is None or not Path(db_path).exists():
        return {"results": [], "note": "no records database for this profile"}
    if not allowed:
        return {"results": [], "note": f"this role may not see {m.noun} records"}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)      # read-only
    try:
        matches = find_subjects(db, m, query)
        if len(matches) > 1:
            return {"results": [], "note": f"more than one {m.noun} matches; use the full name"}
        results = subject_records(db, m, *matches[0], allowed) if matches else []
        extra = {"status": m.status} if m.status else {}
        return {"results": [{**r, **extra} for r in results[:m.max_results]]}
    finally:
        db.close()


@mcp.tool()
def search_records(query: str, allowed_classes: list[str]) -> dict:
    """Find the person named in the question and return the records the caller's role
    may see (allowed_classes), with source IDs."""
    return search(PROFILE.records_db, query, allowed_classes, PROFILE.records_map)


if __name__ == "__main__":
    db_path = PROFILE.records_db
    if PROFILE.records_map is None or db_path is None:
        print(f"Records for profile {PROFILE.name}: none (no records_db / records_map in the profile)")
    elif not db_path.exists():
        print(f"Records for profile {PROFILE.name}: none (build it: see the profile's data/README.md)")
    else:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            problems = check_schema(conn, PROFILE.records_map)
        if problems:
            # Stop at startup, not halfway through a request.
            sys.exit(f"records.yaml does not match {db_path}:\n  " + "\n  ".join(problems))
        print(f"Records for profile {PROFILE.name}: {db_path} "
              f"({PROFILE.records_map.noun}s, {len(PROFILE.records_map.records)} record types)")
    mcp.run(transport="streamable-http")