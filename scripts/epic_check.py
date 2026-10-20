"""Check the connection to Epic's FHIR sandbox, step by step (ADR 013).

    .venv/bin/python scripts/epic_check.py
    .venv/bin/python scripts/epic_check.py --patient "Camila Lopez 1987-09-12"

1. Sign in: a signed assertion (your private key) is exchanged for an access token.
2. The server: its CapabilityStatement (FHIR version, software).
3. Optional: find a test patient by name, and count what the profile would read for them.

Reads EPIC_CLIENT_ID and EPIC_PRIVATE_KEY_PATH from .env. Prints no token and no key. Read-only:
the only request to the FHIR server is GET.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fhir.client import FhirError, client_from_settings  # noqa: E402
from profiles.loader import load_profile  # noqa: E402


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="clinic-epic")
    ap.add_argument("--patient", help='a sandbox test patient with date of birth, e.g. "Camila Lopez 1987-09-12"')
    args = ap.parse_args()
    load_env(ROOT / ".env")
    m = load_profile(args.profile).fhir_map
    if m is None:
        print(f"Profile {args.profile} has no fhir.yaml")
        return 1

    print("1. Signing in to the authorization server")
    try:
        client = client_from_settings(m.server)
        client.auth.token()
        print(f"   ✓ access token received (scope: {getattr(client.auth, 'scope_granted', None) or 'not stated'})")
    except FhirError as e:
        print(f"   ✗ {e}")
        print("   If Epic says the client is invalid or the key can't be verified: the app or its key may not be\n"
              "   active in the sandbox yet (this can take hours after saving), the client ID in .env may be the\n"
              "   production one, or the JWK Set URL may be wrong. See docs/epic/README.md.")
        return 1

    print("2. Reading the server's capabilities")
    try:
        meta = client.capability()
        kinds = {r["type"] for rest in meta.get("rest", []) for r in rest.get("resource", [])}
        need = {m.resource, *(r.resource for r in m.records)}
        print(f"   ✓ FHIR {meta.get('fhirVersion', '?')}, {(meta.get('software') or {}).get('name', 'server')}; "
              f"resource types this profile reads: "
              + ", ".join(f"{k} {'✓' if k in kinds else '✗ not offered'}" for k in sorted(need)))
    except FhirError as e:
        print(f"   ✗ {e}")
        return 1

    if args.patient:
        from mcp_servers.fhir_records.server import find_patient
        from profiles.fhir_map import human_name
        print(f"3. Finding {args.patient}")
        try:
            p, why = find_patient(client, m, args.patient)
            if p is None:
                print("   ✗ " + ("add the date of birth, e.g. \"Camila Lopez 1987-09-12\"" if why == "birthdate" else
                                  f"{why} found (check the name and date of birth against Epic's test patients)"))
                return 1
            print(f"   ✓ {human_name(p)} (Patient {p['id'][:6]}…)")
            for r in m.records:
                found = client.search(r.resource, {r.link: p["id"], **dict(r.search), "_count": r.limit}, r.limit)
                print(f"   ✓ {r.name}: {len(found)} {r.resource} record(s)")
        except FhirError as e:
            print(f"   ✗ {e}")
            return 1
    print("Connected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
