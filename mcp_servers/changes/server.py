"""MCP server that makes approved changes in the active profile's records database.

General-purpose: what each change does comes from the `change:` block of an action in the
profile's actions.yaml (profiles/changes.py). For each such action it offers one tool,
named after the action's tool (the clinic's reschedule_appointment, the county's
release_payment), so OPA, roles and the audit still see each change by name.

It is the only code that writes to the records, and only the executor agent may call it
(profile and OPA), after a person approved the change (policies/actions.rego). The caller
sends the proposal's field values and the approval ID, nothing else: the table, the
record, the columns written and the precondition come from the profile. Values go in as
parameters, never into the SQL text.

Each change is recorded in an action log in the same transaction, keyed by its approval
ID, so a change is made at most once, even if the executor sends it again after a restart.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from profiles.loader import Action, Profile, load_profile
from profiles.records_map import render

PROFILE = load_profile()

mcp = FastMCP("changes", host="127.0.0.1", port=8102)

ACTION_LOG = """
CREATE TABLE IF NOT EXISTS action_log (
    approval_id  TEXT PRIMARY KEY,      -- one change per approval, never two
    action       TEXT NOT NULL,
    target       TEXT NOT NULL,
    before       TEXT,                  -- JSON: the written columns before the change
    after        TEXT,                  -- JSON: the written columns after the change
    done_at      TEXT NOT NULL
)"""


def _q(name: str) -> str:
    """Quote a table or column name. Names are already checked to be plain identifiers."""
    return f'"{name}"'


def _checked(action: Action, fields: dict) -> tuple[dict | None, str | None]:
    """Check the proposal's values again here: the server trusts nothing it is sent."""
    out = {}
    for name, spec in action.fields.items():
        value = (fields or {}).get(name)
        if value in (None, ""):
            return None, f"no {name}"
        value = str(value).strip()
        if spec.get("pattern") and not spec["pattern"].search(value):
            return None, f"{name} '{value}' does not look right"
        if spec["type"] == "datetime":
            try:
                value = datetime.fromisoformat(value.replace(" ", "T")).isoformat(timespec="minutes")
            except ValueError:
                return None, f"{name} is not a date and time"
        out[name] = value[:500]
    return out, None


def _summary(action: Action, cited: str, before: dict, after: dict) -> str:
    ch = action.change
    if ch.done_text:
        row = {"id": cited, **{f"before_{c}": v for c, v in before.items()},
               **{f"after_{c}": v for c, v in after.items()}}
        return render(ch.done_text.text, row)
    return f"{cited}: " + "; ".join(f"{c} {before.get(c)} -> {after.get(c)}" for c in ch.columns())


def apply(db_path: Path | None, action: Action, fields: dict, approval_id: str) -> dict:
    """Make one approved change, exactly as the profile describes it, at most once."""
    ch = action.change
    if ch is None:
        return {"status": "failed", "reason": f"action {action.name} has no change defined"}
    if db_path is None or not Path(db_path).exists():
        return {"status": "failed", "reason": "no records database for this profile"}
    if not approval_id:
        return {"status": "failed", "reason": "no approval ID"}
    values, error = _checked(action, fields)
    if error:
        return {"status": "failed", "reason": error}
    cited = values[ch.id_field]
    stored = ch.stored_id(cited)
    if stored is None:
        return {"status": "failed", "reason": f"{cited} is not a {ch.record} ID"}
    new = {**dict(ch.set_values), **{c: values[f] for c, f in ch.set_from}}

    db = sqlite3.connect(db_path)
    try:
        db.execute(ACTION_LOG)
        done = db.execute("SELECT target, before, after FROM action_log WHERE approval_id = ?",
                          (approval_id,)).fetchone()
        if done:
            before, after = json.loads(done[1]), json.loads(done[2])
            return {"status": "already_done", "action": action.name, "record_id": done[0],
                    "before": before, "after": after, "summary": _summary(action, done[0], before, after)}
        read = list(dict.fromkeys(ch.columns() + [c for c, _ in ch.only_if]))
        row = db.execute(f"SELECT {', '.join(_q(c) for c in read)} FROM {_q(ch.table)} "
                         f"WHERE {_q(ch.id_column)} = ?", (stored,)).fetchone()
        if row is None:
            return {"status": "failed", "reason": f"{cited} not found"}
        current = dict(zip(read, row))
        for col, expected in ch.only_if:
            if current[col] != expected:
                return {"status": "failed", "reason": f"{cited} can't be changed: {col} is "
                                                      f"{current[col]}, not {expected}"}
        before = {c: current[c] for c in ch.columns()}
        sets = ", ".join(f"{_q(c)} = ?" for c in new)
        conds = " AND ".join([f"{_q(ch.id_column)} = ?"] + [f"{_q(c)} IS ?" for c, _ in ch.only_if])
        with db:                                   # one transaction: the change and its log entry
            cur = db.execute(f"UPDATE {_q(ch.table)} SET {sets} WHERE {conds}",
                             [*new.values(), stored, *[v for _, v in ch.only_if]])
            if cur.rowcount != 1:                  # changed by someone else in the meantime
                raise sqlite3.IntegrityError("the record changed before the update")
            db.execute("INSERT INTO action_log VALUES (?, ?, ?, ?, ?, ?)",
                       (approval_id, action.name, cited, json.dumps(before), json.dumps(new),
                        datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return {"status": "done", "action": action.name, "record_id": cited, "before": before,
                "after": new, "summary": _summary(action, cited, before, new)}
    except sqlite3.IntegrityError as e:
        return {"status": "failed", "reason": str(e)}
    finally:
        db.close()


def check_changes(db: sqlite3.Connection, profile: Profile) -> list[str]:
    """Problems between the profile's changes and the database: missing tables or columns."""
    problems = []
    for a in profile.actions.values():
        if a.change is None:
            continue
        ch = a.change
        have = {row[1] for row in db.execute(f"PRAGMA table_info({_q(ch.table)})")}
        if not have:
            problems.append(f"action {a.name}: table '{ch.table}' is not in the database")
            continue
        need = [ch.id_column, *ch.columns(), *(c for c, _ in ch.only_if)]
        problems += [f"action {a.name}: column '{ch.table}.{c}' is not in the database"
                     for c in dict.fromkeys(need) if c not in have]
    return problems


def _register(action: Action) -> None:
    def change_tool(fields: dict, approval_id: str) -> dict:
        return apply(PROFILE.records_db, action, fields, approval_id)
    mcp.add_tool(change_tool, name=action.tool,
                 description=f"{action.title}. Only for changes a person has approved.")


CHANGE_ACTIONS = [a for a in PROFILE.actions.values() if a.change is not None]
for _a in CHANGE_ACTIONS:
    _register(_a)


if __name__ == "__main__":
    db_path = PROFILE.records_db
    names = ", ".join(a.tool for a in CHANGE_ACTIONS) or "none"
    if CHANGE_ACTIONS and db_path and db_path.exists():
        with sqlite3.connect(db_path) as conn:
            problems = check_changes(conn, PROFILE)
        if problems:
            # Stop at startup, not halfway through an approved change.
            sys.exit(f"actions.yaml does not match {db_path}:\n  " + "\n  ".join(problems))
    print(f"Changes for profile {PROFILE.name}: {names}")
    mcp.run(transport="streamable-http")