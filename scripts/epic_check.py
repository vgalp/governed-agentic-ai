"""Check the connection to Epic's FHIR sandbox, step by step (ADR 013).

    .venv/bin/python scripts/epic_check.py
    .venv/bin/python scripts/epic_check.py --patient "Camila Lopez 1987-09-12"

The same as scripts/fhir_check.py with --profile clinic-epic (kept so existing instructions work).
Reads EPIC_CLIENT_ID and EPIC_PRIVATE_KEY_PATH from .env. Prints no token and no key. Read-only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.fhir_check import load_env, main  # noqa: E402,F401  (load_env: used by fetch_drug_labels.py)

if __name__ == "__main__":
    sys.exit(main("clinic-epic"))
