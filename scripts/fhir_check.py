"""Check the connection to a profile's FHIR server, step by step (ADR 013, ADR 032).

    .venv/bin/python scripts/fhir_check.py --profile clinic-oracle
    .venv/bin/python scripts/fhir_check.py --profile clinic-oracle --patient "Wilm Smart 1990-09-15"
    .venv/bin/python scripts/fhir_check.py --profile clinic-oracle --patient-id 12724065
    .venv/bin/python scripts/fhir_check.py --profile clinic-meditech        # the signed-in patient
    .venv/bin/python scripts/fhir_check.py --profile clinic-epic --patient "Camila Lopez 1987-09-12"

1. Sign in, as the profile's fhir.yaml says: none (an open server), the app itself (smart_backend:
   a signed assertion or a client secret), or a person who signed in with scripts/fhir_connect.py
   (smart_user).
2. The server: its CapabilityStatement (FHIR version, software, the resource types it offers).
3. Optional: find a test patient by name and date of birth, or read one by id, and count what the
   profile would read for them. With smart_user, the patient the sign-in is for is used by default.

Reads client IDs, keys and secrets from .env. Prints no token, key or secret. Read-only: the only
request to the FHIR server is GET.
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


SIGN_IN_HELP = {
    "smart_backend": "If the server says the client is invalid or the key can't be verified: the app or its key\n"
                     "   may not be active yet (this can take hours after saving), the client ID may be for another\n"
                     "   environment, or the JWK Set URL may be wrong.",
    "smart_user": "Sign in first: .venv/bin/python scripts/fhir_connect.py --profile {profile}",
}


def main(default_profile: str | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=default_profile, required=default_profile is None)
    ap.add_argument("--patient", help='a test patient with date of birth, e.g. "Wilm Smart 1990-09-15"')
    ap.add_argument("--patient-id", help="read a test patient by its FHIR id instead of searching by name")
    args = ap.parse_args()
    load_env(ROOT / ".env")
    m = load_profile(args.profile).fhir_map
    if m is None:
        print(f"Profile {args.profile} has no fhir.yaml")
        return 1
    kind = m.server.get("auth", "none")
    docs = m.server.get("docs", "docs/epic/README.md")

    print(f"1. Signing in ({kind})")
    try:
        client = client_from_settings(m.server)
        if client.auth is None:
            print("   ✓ open server: no sign-in")
        else:
            client.auth.token()
            print(f"   ✓ access token received (scope: {getattr(client.auth, 'scope_granted', None) or 'not stated'})")
    except FhirError as e:
        print(f"   ✗ {e}")
        if kind in SIGN_IN_HELP:
            print("   " + SIGN_IN_HELP[kind].format(profile=args.profile))
        print(f"   See {docs}.")
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

    pid = args.patient_id or (getattr(client.auth, "patient", None) if not args.patient else None)
    if args.patient or pid:
        from mcp_servers.fhir_records.server import find_patient
        from profiles.fhir_map import human_name
        try:
            if pid:
                print(f"3. Reading Patient {pid[:6]}…" + (" (the signed-in patient)" if not args.patient_id else ""))
                p = client.read(m.resource, pid)
            else:
                print(f"3. Finding {args.patient}")
                p, why = find_patient(client, m, args.patient)
                if p is None and why == "more than one":
                    from mcp_servers.fhir_records.server import birthdates_in, name_candidates
                    given, family = name_candidates(args.patient)[0]
                    seen = client.search(m.resource, {m.name_search["given"]: given, m.name_search["family"]: family,
                                                      **({m.name_search["birthdate"]: birthdates_in(args.patient)[0]}
                                                         if m.needs_birthdate else {})}, 10)
                    print("   ✗ more than one patient has exactly that name and date of birth:")
                    for r in seen:
                        print(f"     Patient {r.get('id')}: {human_name(r)}, born {r.get('birthDate', '?')}")
                    print("   Use --patient-id with one of them. The assistant will not choose between them.")
                    return 1
                if p is None:
                    print("   ✗ " + ("add the date of birth, e.g. \"Wilm Smart 1990-09-15\"" if why == "birthdate" else
                                      f"{why} found (check the name and date of birth against the server's test patients)"))
                    return 1
            n = (p.get("name") or [{}])[0]
            print(f"   ✓ {human_name(p)} (Patient {p['id'][:6]}…); for a question: given "
                  f"{' '.join(n.get('given') or []) or '?'}, family {n.get('family') or '?'}, "
                  f"born {p.get('birthDate', '?')}")
            for r in m.records:
                try:
                    found = client.search(r.resource, {r.link: p["id"], **dict(r.search), "_count": r.limit}, r.limit)
                    print(f"   ✓ {r.name}: {len(found)} {r.resource} record(s)")
                except FhirError as e:          # one refused search should not hide the others
                    print(f"   ✗ {r.name}: {e}")
        except FhirError as e:
            print(f"   ✗ {e}")
            return 1
    print("Connected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
