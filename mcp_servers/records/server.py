"""MCP server exposing a read-only search over the active profile's patient records
(profiles/<name>/data/clinic.db, built from synthetic Synthea data).

It finds the patient named in the question and returns their records, each with a
source ID the answer can cite: pt-, apt-, med-, enc-, clm-. It runs locally and is
the only tool that receives real names; the gateway audits the masked question.

Records are built only from the data classes the caller's role may see (decided by
OPA and passed in by the gateway). Each record lists the classes it contains, so the
gateway can check it again. Data a role may not see is never read.
"""

import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from profiles.loader import load_profile

PROFILE = load_profile()
MAX_RESULTS = 15

mcp = FastMCP("records", host="127.0.0.1", port=8101)


def _short(record_id: str) -> str:
    """Synthea IDs share a prefix per patient; the last 8 characters tell rows apart."""
    return record_id[-8:]


def find_patients(db: sqlite3.Connection, query: str) -> list[tuple[str, str]]:
    """Patients named in the query: full name first, else a last name only one patient has."""
    q = query.lower()
    patients = db.execute("SELECT id, name FROM patients").fetchall()
    full = [(pid, name) for pid, name in patients if name.lower() in q]
    if full:
        return full
    words = set(re.findall(r"[a-z'-]+", q))
    by_last = [(pid, name) for pid, name in patients if name.split()[-1].lower() in words]
    return by_last if len(by_last) == 1 else []


def _record(rid: str, title: str, text: str, classes: list[str]) -> dict:
    return {"id": rid, "title": title, "text": text, "data_classes": classes}


def patient_records(db: sqlite3.Connection, pid: str, name: str, allowed: set[str]) -> list[dict]:
    """Build the patient's records from the allowed data classes only."""
    out = []
    if "patient_details" in allowed:
        birth, sex, city = db.execute("SELECT birth_date, sex, city FROM patients WHERE id = ?", (pid,)).fetchone()
        out.append(_record(f"pt-{_short(pid)}", f"Patient: {name}",
                           f"{name}, born {birth}, sex {sex}, lives in {city}.", ["patient_details"]))

    if "appointments" in allowed:
        for aid, starts_at, typ, status in db.execute(
                "SELECT id, starts_at, type, status FROM appointments WHERE patient_id = ? ORDER BY starts_at", (pid,)):
            out.append(_record(aid, "Upcoming appointment",
                               f"{name}: {typ} on {starts_at.replace('T', ' at ')} ({status}).", ["appointments"]))

    if "medications" in allowed:
        for mid, start, desc, reason in db.execute(
                "SELECT id, start, description, reason FROM medications "
                "WHERE patient_id = ? AND stop IS NULL ORDER BY start DESC", (pid,)):
            why = f" Reason recorded: {reason}." if reason else ""
            out.append(_record(f"med-{mid}", "Current medication",
                               f"{name}: {desc}, on the list since {start}.{why}", ["medications"]))

    # Visits: dates for anyone with visit_dates; what the visit was for only with visit_clinical.
    if "visit_dates" in allowed:
        clinical = "visit_clinical" in allowed
        for eid, start, typ, desc, reason in db.execute(
                "SELECT id, start, type, description, reason FROM encounters "
                "WHERE patient_id = ? ORDER BY start DESC LIMIT 3", (pid,)):
            text = f"{name}: {typ} visit on {start[:10]}."
            classes = ["visit_dates"]
            if clinical:
                why = f" Reason recorded: {reason}." if reason else ""
                text = f"{name}: {typ} visit on {start[:10]}, {desc}.{why}"
                classes.append("visit_clinical")
            out.append(_record(f"enc-{_short(eid)}", "Past visit", text, classes))

    if "claims" in allowed:
        for cid, date, amount, paid, status, owed in db.execute(
                "SELECT id, service_date, amount, insurance_paid, patient_status, patient_outstanding "
                "FROM claims WHERE patient_id = ? ORDER BY patient_outstanding DESC, service_date DESC LIMIT 3",
                (pid,)):
            out.append(_record(f"clm-{_short(cid)}", "Claim",
                               f"{name}: visit on {date}, amount {amount or 0:.2f}, insurance paid {paid or 0:.2f}, "
                               f"patient status {status}, patient still owes {owed or 0:.2f}.", ["claims"]))
    return out


def search(db_path: Path | None, query: str, allowed_classes: list[str]) -> dict:
    allowed = set(allowed_classes)
    if db_path is None or not db_path.exists():
        return {"results": [], "note": "no records database for this profile"}
    if not allowed:
        return {"results": [], "note": "this role may not see patient records"}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)      # read-only
    try:
        matches = find_patients(db, query)
        if len(matches) > 1:
            return {"results": [], "note": "more than one patient matches; use the full name"}
        results = patient_records(db, *matches[0], allowed) if matches else []
        return {"results": [{**r, "status": "synthetic record"} for r in results[:MAX_RESULTS]]}
    finally:
        db.close()


@mcp.tool()
def search_records(query: str, allowed_classes: list[str]) -> dict:
    """Find the patient named in the question and return the records the caller's role
    may see (allowed_classes), with source IDs."""
    return search(PROFILE.records_db, query, allowed_classes)


if __name__ == "__main__":
    db = PROFILE.records_db
    state = f"{db}" if db and db.exists() else "none (build it: see the profile's data/README.md)"
    print(f"Records for profile {PROFILE.name}: {state}")
    mcp.run(transport="streamable-http")