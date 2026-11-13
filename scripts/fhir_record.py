"""Record a FHIR sandbox's answers for replay in the tests (ADR 036).

    .venv/bin/python scripts/fhir_record.py --profile clinic-oracle --patient-id 12724065 \\
        --patient "Wilm Smart 1990-09-15" --out tests/fixtures/fhir/oracle
    .venv/bin/python scripts/fhir_record.py --profile clinic-epic --patient-id <Camila Lopez's id> \\
        --patient "Camila Lopez 1987-09-12" --out tests/fixtures/fhir/epic

Signs in as the profile says (like scripts/fhir_check.py), then makes the requests the assistant
makes: the CapabilityStatement, the patient by id, the patient by name and birth date, and each
record type the profile reads, following every page. Each answer is written to the folder; the
next test run replays them (tests/test_fhir_replay.py).

Sandboxes only: the script refuses any server not in fhir/recording.SANDBOX_HOSTS, because a
recording is committed to the repository and must hold synthetic test patients only. Tokens and
headers other than Retry-After are never written. Read-only: GET to the FHIR server, and the token
request.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fhir.client import FhirError, client_from_settings  # noqa: E402
from fhir.recording import RecordingSession, is_sandbox  # noqa: E402
from fhir.transport import pooled_session  # noqa: E402
from profiles.loader import load_profile  # noqa: E402
from scripts.fhir_check import load_env  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--patient-id", required=True, help="a sandbox test patient's FHIR id")
    ap.add_argument("--patient", help='the same patient by name and birth date, e.g. "Wilm Smart 1990-09-15"')
    ap.add_argument("--out", required=True, type=Path, help="folder to write, e.g. tests/fixtures/fhir/oracle")
    args = ap.parse_args()
    load_env(ROOT / ".env")

    m = load_profile(args.profile).fhir_map
    if m is None:
        print(f"Profile {args.profile} has no fhir.yaml")
        return 1
    base = m.server["base_url"]
    if not is_sandbox(base):
        print(f"Refusing to record {base}: recordings are committed, so only vendor sandboxes with synthetic "
              "patients may be recorded (fhir/recording.SANDBOX_HOSTS).")
        return 1

    recorder = RecordingSession(pooled_session(4), base)
    client = client_from_settings(m.server, session=recorder)
    today = dt.date.today()
    try:
        client.capability()
        patient = client.read(m.resource, args.patient_id)
        print(f"✓ Patient {args.patient_id[:6]}…")
        if args.patient:
            from mcp_servers.fhir_records.server import find_patient
            found, why = find_patient(client, m, args.patient)
            print("✓ found by name and birth date" if found else f"! not found by name and birth date ({why})")
        for r in m.records:
            try:
                found, more = client.search_all(r.resource, r.params(patient["id"], today), r.limit)
                print(f"✓ {r.name}: {len(found)} {r.resource}" + (" (more on the server)" if more else ""))
            except FhirError as e:                     # a refusal is worth recording too
                print(f"! {r.name}: {e}")
    except FhirError as e:
        print(f"✗ {e}")
        return 1

    path = recorder.save(args.out, {
        "source": "recorded",
        "recorded_on": today.isoformat(),
        "profile": args.profile,
        "patient": {"id": args.patient_id, "query": args.patient},
        "note": "Recorded from the vendor's sandbox: synthetic test patients only.",
    })
    print(f"Wrote {len(recorder.exchanges)} answers to {path.parent}. Run: pytest tests/test_fhir_replay.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
