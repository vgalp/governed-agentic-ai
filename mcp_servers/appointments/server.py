"""MCP server that makes approved appointment changes in the active profile's records
(profiles/<name>/data/clinic.db, synthetic data).

It is the only code that writes to the records, and only the executor agent may call it
(profile and OPA), after a person approved the change (policies/actions.rego). Each change
carries its approval ID and is recorded in an action log in the same transaction, so a
change is made at most once, even if the executor sends it again after a restart.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from profiles.loader import load_profile

PROFILE = load_profile()

mcp = FastMCP("appointments", host="127.0.0.1", port=8102)

ACTION_LOG = """
CREATE TABLE IF NOT EXISTS action_log (
    approval_id  TEXT PRIMARY KEY,      -- one change per approval, never two
    action       TEXT NOT NULL,
    target       TEXT NOT NULL,
    before       TEXT,
    after        TEXT,
    done_at      TEXT NOT NULL
)"""


def reschedule(db_path: Path | None, appointment_id: str, new_start: str, approval_id: str) -> dict:
    if db_path is None or not db_path.exists():
        return {"status": "failed", "reason": "no records database for this profile"}
    if not approval_id:
        return {"status": "failed", "reason": "no approval ID"}
    try:
        new = datetime.fromisoformat(new_start).isoformat(timespec="minutes")
    except ValueError:
        return {"status": "failed", "reason": "new_start is not a date and time"}
    db = sqlite3.connect(db_path)
    try:
        db.execute(ACTION_LOG)
        done = db.execute("SELECT target, before, after FROM action_log WHERE approval_id = ?",
                          (approval_id,)).fetchone()
        if done:
            return {"status": "already_done", "appointment_id": done[0], "before": done[1], "after": done[2]}
        row = db.execute("SELECT starts_at, status FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        if row is None:
            return {"status": "failed", "reason": f"appointment {appointment_id} not found"}
        before, status = row
        if status != "scheduled":
            return {"status": "failed", "reason": f"appointment {appointment_id} is {status}"}
        with db:                                   # one transaction: the change and its log entry
            db.execute("UPDATE appointments SET starts_at = ? WHERE id = ?", (new, appointment_id))
            db.execute("INSERT INTO action_log VALUES (?, ?, ?, ?, ?, ?)",
                       (approval_id, "reschedule_appointment", appointment_id, before, new,
                        datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return {"status": "done", "appointment_id": appointment_id, "before": before, "after": new}
    finally:
        db.close()


@mcp.tool()
def reschedule_appointment(appointment_id: str, new_start: str, approval_id: str) -> dict:
    """Move an appointment to a new start time. Only for changes a person has approved."""
    return reschedule(PROFILE.records_db, appointment_id, new_start, approval_id)


if __name__ == "__main__":
    db = PROFILE.records_db
    state = f"{db}" if db and db.exists() else "none (build it: see the profile's data/README.md)"
    print(f"Appointment changes for profile {PROFILE.name}: {state}")
    mcp.run(transport="streamable-http")